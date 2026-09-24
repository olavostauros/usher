"""``create_app``: the events API as a FastAPI application (spec §3).

Everything is ``GET`` and JSON. Errors use one envelope
(``{"error": {"code", "message", "field"}}``, ``docs/api/README.md``), the
database being down is a 503, and CORS is an allowlist from
``EVCRAWL_API_CORS_ORIGINS`` (``GET`` only, no credentials). Each request
logs one line: method, route template, status, duration, row count. Never
the query string, never the client address: a user's position and IP are
personal data (LGPD), and neither is logged or stored.
"""

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from evcrawl.api.db import ApiDatabase, DatabaseNotConfigured, Reader
from evcrawl.api.responses import error
from evcrawl.api.routes import catalog, health
from evcrawl.config import Settings, get_settings
from evcrawl.logging import get_logger

TITLE = "Usher events API"
API_VERSION = "1"
DESCRIPTION = (
    "Read-only upcoming events in Brazil, crawled by Evento Livre. "
    "The prose contract is docs/api/README.md."
)

log = get_logger("evcrawl.api")

_STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    410: "gone",
    429: "rate_limited",
}


def cors_origins(settings: Settings) -> list[str]:
    return [o.strip() for o in settings.evcrawl_api_cors_origins.split(",") if o.strip()]


def create_app(db: Reader | None = None, settings: Settings | None = None) -> FastAPI:
    """The app. ``db`` defaults to ``api_ro`` on ``EVCRAWL_API_DATABASE_URL``, opened lazily."""
    settings = settings or get_settings()
    app = FastAPI(
        title=TITLE,
        version=API_VERSION,
        description=DESCRIPTION,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db = db if db is not None else ApiDatabase(settings.evcrawl_api_database_url)
    app.include_router(health.router)
    app.include_router(catalog.router)

    origins = cors_origins(settings)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET"],
            allow_credentials=False,
        )

    @app.middleware("http")
    async def log_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        log.info(
            "api.request",
            method=request.method,
            route=getattr(route, "path", None),
            status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1),
            rows=getattr(request.state, "rows", None),
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_parameter(request: Request, exc: RequestValidationError) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(part) for part in first.get("loc", ()) if part not in ("query", "path")]
        return error(
            422,
            "invalid_parameter",
            str(first.get("msg", "invalid parameter")),
            loc[-1] if loc else None,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        return error(exc.status_code, _STATUS_CODES.get(exc.status_code, "error"), exc.detail)

    @app.exception_handler(OperationalError)
    async def db_unavailable(request: Request, exc: OperationalError) -> Response:
        # Connection refused, timeout (statement_timeout included): the class only,
        # the message may carry the query and its parameters.
        log.warning("api.db_unavailable", error=type(exc.orig).__name__)
        return error(503, "db_unavailable", "The database is not answering; retry later.")

    @app.exception_handler(DatabaseNotConfigured)
    async def db_not_configured(request: Request, exc: DatabaseNotConfigured) -> Response:
        return error(503, "db_unavailable", "The database is not configured.")

    return app
