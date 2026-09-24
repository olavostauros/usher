"""The API's database access: ``api_ro``, read-only sessions, SQL on the ``api_*`` views.

The engine comes from ``EVCRAWL_API_DATABASE_URL``, never from ``DATABASE_URL``
(spec §3): the API process must not hold the pipeline's credentials. Every
session is read-only with a 3 s statement timeout, set at connection start;
the ``api_ro`` role carries the same settings (``evcrawl.db.roles.API``), so
either alone holds. Queries are SQLAlchemy Core ``text()`` on the sync engine,
run in the thread pool so they never block the event loop.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import Engine, RowMapping, create_engine, text
from starlette.concurrency import run_in_threadpool

SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=3s"
# Below api_ro's connection limit (API_CONNECTION_LIMIT, 16), leaving room for psql.
POOL_SIZE = 5
MAX_OVERFLOW = 5


class DatabaseNotConfigured(RuntimeError):
    """``EVCRAWL_API_DATABASE_URL`` is unset; the API has no database to read."""


class Reader(Protocol):
    """What a route needs from the database: rows of one ``SELECT``."""

    async def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[RowMapping]:
        """Run ``sql`` with ``params`` and return every row as a mapping."""
        ...


def api_engine(url: str) -> Engine:
    """An engine whose every connection is read-only with the 3 s timeout."""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        connect_args={"options": SESSION_OPTIONS},
    )


class ApiDatabase:
    """``Reader`` on an engine, built lazily so the app (and its OpenAPI) needs no database."""

    def __init__(self, url: str | None = None, *, engine: Engine | None = None) -> None:
        self._url = url
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            if not self._url:
                raise DatabaseNotConfigured("EVCRAWL_API_DATABASE_URL is not set")
            self._engine = api_engine(self._url)
        return self._engine

    def _rows(self, sql: str, params: Mapping[str, Any] | None) -> list[RowMapping]:
        with self.engine.connect() as conn:
            return list(conn.execute(text(sql), dict(params or {})).mappings())

    async def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[RowMapping]:
        return await run_in_threadpool(self._rows, sql, params)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
