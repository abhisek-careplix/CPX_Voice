"""CarePlix Voice control-plane API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from careplix_api import (
    connections_api,
    integrations_api,
    internal_api,
    scenarios_api,
    seed,
    studio_api,
)
from careplix_api.browser import api as browser_api
from careplix_api.browser.runtime_client import HttpRuntime
from careplix_api.browser.service import BrowserRunner
from careplix_api.calls_service import CallRunner
from careplix_api.db import Database
from careplix_api.errors import ApiError
from careplix_api.evals import api as evals_api
from careplix_api.evals import service as evals_service
from careplix_api.integrations.service import IntegrationWorker
from careplix_api.judge import service as judge_service
from careplix_api.livekit_gateway import LiveKitGateway
from careplix_api.practice import api as practice_api
from careplix_api.practice import webhooks as practice_webhooks
from careplix_api.practice.connections import ensure_practice_connections
from careplix_api.practice.worker import PracticeWorker
from careplix_api.settings import Settings, get_settings
from careplix_api.shadow import api as shadow_api
from careplix_api.shadow import service as shadow_service
from careplix_common import safe_logging
from careplix_common.gemini import probe_live

log = logging.getLogger("careplix.api")


def _error(request: Request, status: int, code: str, message: str, detail: dict | None = None):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    body = {"code": code, "message": message, "request_id": request_id}
    if detail:
        body["detail"] = detail
    return JSONResponse(
        status_code=status, content={"error": body}, headers={"X-Request-ID": request_id}
    )


def create_app(
    settings: Settings | None = None,
    gateway: LiveKitGateway | None = None,
    model_prober=probe_live,
    integration_transport=None,
    judge=None,
    browser_runtime=None,
    browser_agent_factory=None,
) -> FastAPI:
    settings = settings or get_settings()
    safe_logging.configure(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings.database_url)
        app.state.gateway = gateway or LiveKitGateway(settings)
        app.state.runner = CallRunner(app.state.db, app.state.gateway, settings)
        app.state.model_prober = model_prober
        app.state.judge = judge or judge_service.default_judge
        app.state.settings = settings
        app.state.browser_runtime = browser_runtime or HttpRuntime(
            settings.browser_runtime_url,
            settings.browser_service_token.get_secret_value(),
            settings.browser_runtime_auth,
        )
        app.state.browser_runner = BrowserRunner(
            app.state.db, settings, app.state.browser_runtime, browser_agent_factory
        )
        # Tests inject an in-process transport (e.g. the payer sandbox ASGI app).
        app.state.integration_transport = integration_transport
        await app.state.db.create_all()
        if settings.seed_demo_data:
            async with app.state.db.sessions() as session:
                await seed.seed(session, settings)
                await seed.ensure_dev_integrations(session, settings)
            async with app.state.db.sessions() as session:
                await seed.ensure_reference_connections(session)
        async with app.state.db.sessions() as session:
            await ensure_practice_connections(session, settings)
        if settings.run_background:
            await app.state.browser_runner.recover()
        app.state.integration_worker = IntegrationWorker(
            app.state.db, lambda: app.state.integration_transport
        )
        app.state.practice_worker = PracticeWorker(
            app.state.db, settings, lambda: app.state.integration_transport
        )
        # One service runs the background loops; others (e.g. the worker-facing internal API) don't.
        loops = (
            [
                asyncio.create_task(app.state.runner.reconcile_forever()),
                asyncio.create_task(app.state.integration_worker.run_forever()),
                asyncio.create_task(app.state.practice_worker.run_forever()),
                asyncio.create_task(
                    judge_service.judge_forever(app.state.db, settings, app.state.judge)
                ),
                asyncio.create_task(
                    evals_service.run_forever(app.state.db, settings, app.state.gateway)
                ),
                asyncio.create_task(
                    shadow_service.run_forever(
                        app.state.db, settings, app.state.gateway, app.state.runner
                    )
                ),
            ]
            if settings.run_background
            else []
        )
        yield
        for loop in loops:
            loop.cancel()
        # Let each loop finish cancelling before the database closes under it; a loop cancelled
        # mid-query would otherwise wait forever on a connection that's gone.
        await asyncio.gather(*loops, return_exceptions=True)
        await app.state.runner.aclose()
        await app.state.browser_runner.aclose()
        await app.state.db.dispose()

    app = FastAPI(title="CarePlix Voice API", version="0.2.0", lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        # Always server-generated: a client-supplied ID is never trusted for correlation.
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return _error(request, exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return _error(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error(request, 422, "invalid_request", "Request failed validation.")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error(
            "unhandled error",
            exc_info=exc,
            extra={"request_id": request.state.request_id, "error_code": type(exc).__name__},
        )
        return _error(request, 500, "internal_error", "Unexpected error.")

    @app.get("/healthz")
    async def healthz():
        """Process liveness only. Says nothing about telephone, model or database health."""
        return {"status": "ok"}

    app.include_router(studio_api.router, prefix="/api")
    app.include_router(connections_api.router, prefix="/api")
    app.include_router(integrations_api.router, prefix="/api")
    app.include_router(practice_api.router, prefix="/api")
    app.include_router(browser_api.router, prefix="/api")
    app.include_router(scenarios_api.router, prefix="/api")
    app.include_router(evals_api.router, prefix="/api")
    app.include_router(shadow_api.router, prefix="/api")
    app.include_router(internal_api.router)
    if settings.is_dev:
        # Local webhook testing; in the cloud the public ingress is its own service (no IAP).
        app.include_router(practice_webhooks.router)
    if settings.studio_dist:
        _mount_studio(app, settings.studio_dist)
    return app


def _mount_studio(app: FastAPI, dist: str) -> None:
    """Serve the built Studio from the API so the session cookie stays same-origin."""
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    root = Path(dist)
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="studio-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def studio(path: str):
        if path.startswith(("api/", "internal/", "webhooks/")):
            raise StarletteHTTPException(404)
        candidate = root / path
        if path and candidate.is_file() and root in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})


app = create_app()
