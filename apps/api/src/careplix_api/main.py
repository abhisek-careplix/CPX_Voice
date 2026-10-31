"""CarePlix Voice control-plane API (G0 skeleton).

G0 exposes process health and connection readiness only. Agents, tasks, calls and results land in
G1/G2 following BUILD_PLAN §5.4.
"""

from __future__ import annotations

import hmac
import logging
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from careplix_api import readiness
from careplix_api.settings import Settings, get_settings
from careplix_common import safe_logging

log = logging.getLogger("careplix.api")


class ApiError(Exception):
    """Error with a stable code. Bodies never include secrets or sensitive context (doc 06)."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "request_id": request.state.request_id}
        },
        headers={"X-Request-ID": request.state.request_id},
    )


def require_admin(request: Request, settings: Settings = Depends(get_settings)) -> None:
    expected = settings.admin_api_token.get_secret_value()
    if not expected:
        raise ApiError(
            503, "admin_access_not_configured", "Administrative access is not configured."
        )
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise ApiError(401, "unauthenticated", "Valid administrator credentials are required.")


def create_app() -> FastAPI:
    safe_logging.configure(get_settings().log_level)
    app = FastAPI(title="CarePlix Voice API", version="0.1.0")

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        # Always server-generated: a client-supplied ID is never trusted for correlation.
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return _error(request, exc.status, exc.code, exc.message)

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

    @app.get("/readiness", dependencies=[Depends(require_admin)])
    async def get_readiness(settings: Settings = Depends(get_settings)):
        return (await readiness.run_all(settings)).as_dict()

    return app


app = create_app()
