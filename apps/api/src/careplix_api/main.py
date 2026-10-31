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

from careplix_api import internal_api, seed, studio_api
from careplix_api.calls_service import CallRunner
from careplix_api.db import Database
from careplix_api.errors import ApiError
from careplix_api.livekit_gateway import LiveKitGateway
from careplix_api.settings import Settings, get_settings
from careplix_common import safe_logging

log = logging.getLogger("careplix.api")


def _error(request: Request, status: int, code: str, message: str, detail: dict | None = None):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    body = {"code": code, "message": message, "request_id": request_id}
    if detail:
        body["detail"] = detail
    return JSONResponse(
        status_code=status, content={"error": body}, headers={"X-Request-ID": request_id}
    )


def create_app(settings: Settings | None = None, gateway: LiveKitGateway | None = None) -> FastAPI:
    settings = settings or get_settings()
    safe_logging.configure(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings.database_url)
        app.state.gateway = gateway or LiveKitGateway(settings)
        app.state.runner = CallRunner(app.state.db, app.state.gateway, settings)
        await app.state.db.create_all()
        if settings.seed_demo_data:
            async with app.state.db.sessions() as session:
                await seed.seed(session, settings)
        reconciler = asyncio.create_task(app.state.runner.reconcile_forever())
        yield
        reconciler.cancel()
        await app.state.runner.aclose()
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
    app.include_router(internal_api.router)
    return app


app = create_app()
