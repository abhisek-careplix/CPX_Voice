"""HTTP surface of the browser runtime. Private: only the API calls it (Cloud Run IAM plus a
shared token). Nothing here is shown to people directly; the API proxies screens to the Studio."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from functools import cache
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from careplix_browser.runtime import ActionError, Runtime


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    browser_service_token: str = ""
    chromium_path: str = ""  # empty: Playwright's own browser (the container image ships it)


@cache
def get_settings() -> Settings:
    return Settings()


class OpenIn(BaseModel):
    start_url: str = Field(max_length=2000)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)
    storage_state: dict[str, Any] | None = None


class AllowIn(BaseModel):
    hosts: list[str] = Field(max_length=10)


class InspectIn(BaseModel):
    x: int | None = None
    y: int | None = None


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or get_settings()
    rt = runtime or Runtime(executable_path=settings.chromium_path or None)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async def reap():
            while True:
                await asyncio.sleep(60)
                with contextlib.suppress(Exception):
                    await rt.close_idle()

        task = asyncio.create_task(reap())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await rt.shutdown()

    app = FastAPI(title="CarePlix browser runtime", lifespan=lifespan)
    app.state.runtime = rt

    def auth(authorization: str = Header(default="")) -> None:
        token = settings.browser_service_token
        presented = authorization.removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(presented, token):
            raise HTTPException(401, "unauthorized")

    def session_or_404(session_id: str):
        try:
            return rt.get(session_id)
        except KeyError:
            raise HTTPException(404, "session_not_found") from None

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "sessions": len(rt.sessions)}

    @app.post("/sessions", dependencies=[Depends(auth)])
    async def open_session(body: OpenIn):
        try:
            session = await rt.open(body.start_url, body.allowed_hosts, body.storage_state)
        except ActionError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": session.id, "url": session.page.url}

    @app.get("/sessions/{session_id}/screenshot", dependencies=[Depends(auth)])
    async def screenshot(session_id: str, fmt: str = "png"):
        return await rt.screenshot(session_or_404(session_id), "jpeg" if fmt == "jpeg" else "png")

    @app.post("/sessions/{session_id}/inspect", dependencies=[Depends(auth)])
    async def inspect(session_id: str, body: InspectIn):
        return {"element": await rt.inspect(session_or_404(session_id), body.x, body.y)}

    @app.post("/sessions/{session_id}/act", dependencies=[Depends(auth)])
    async def act(session_id: str, action: dict[str, Any]):
        session = session_or_404(session_id)
        try:
            return {"ok": True, **await rt.act(session, action)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc), "url": session.page.url}

    @app.post("/sessions/{session_id}/allow", dependencies=[Depends(auth)])
    async def allow(session_id: str, body: AllowIn):
        session = session_or_404(session_id)
        for host in body.hosts:
            if host and host.lower() not in session.allowed_hosts:
                session.allowed_hosts.append(host.lower())
        return {"allowed_hosts": session.allowed_hosts}

    @app.get("/sessions/{session_id}/storage-state", dependencies=[Depends(auth)])
    async def storage_state(session_id: str):
        return await rt.storage_state(session_or_404(session_id))

    @app.delete("/sessions/{session_id}", dependencies=[Depends(auth)])
    async def close(session_id: str):
        await rt.close(session_id)
        return {"ok": True}

    return app


def app() -> FastAPI:  # uvicorn --factory careplix_browser.app:app
    return create_app()
