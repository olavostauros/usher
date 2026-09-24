"""The events API skeleton (Phase 4 task 2): health, categories, cities, errors, CORS, logs.

Offline tests drive the app with a fake ``Reader``; the DB-backed ones run it
as ``api_ro`` on the scratch database (``tests/test_api_views.py`` seeds it).
"""

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, RowMapping, create_engine, event, text
from sqlalchemy.exc import DBAPIError, OperationalError

from evcrawl import cities
from evcrawl.api.app import create_app
from evcrawl.api.db import SESSION_OPTIONS, ApiDatabase
from evcrawl.cli import openapi_json
from evcrawl.config import Settings
from evcrawl.db.roles import API_ROLE, role_can_login
from evcrawl.logging import configure_logging
from tests.conftest import SCRATCH_URL
from tests.test_api_views import _seed

OPENAPI = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"


class FakeReader:
    """Answers each query by the view it reads; records the SQL it was sent."""

    def __init__(self, tables: Mapping[str, list[dict[str, Any]]], fail: bool = False) -> None:
        self.tables = tables
        self.fail = fail
        self.sql: list[str] = []

    async def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[RowMapping]:
        self.sql.append(sql)
        if self.fail:
            raise OperationalError(sql, params, Exception("connection refused"))
        for view, rows in self.tables.items():
            if view in sql:
                return rows  # type: ignore[return-value]
        return [{"?column?": 1}]  # type: ignore[list-item]


CATEGORIES = [
    {"slug": "music", "name": "Música", "events_upcoming": 812},
    {"slug": "uncategorized", "name": "Sem categoria", "events_upcoming": 40},
]
CITY_COUNTS = [
    {"slug": "vitoria", "events_upcoming": 214},
    {"slug": "sao-paulo", "events_upcoming": 9},
]


def client(reader: FakeReader | ApiDatabase, **settings: Any) -> TestClient:
    return TestClient(create_app(reader, Settings(**settings)))


@pytest.fixture
def fake() -> FakeReader:
    return FakeReader({"api_categories": CATEGORIES, "api_city_counts": CITY_COUNTS})


def test_healthz_reports_the_build_and_the_database(fake: FakeReader) -> None:
    ok = client(fake, evcrawl_git_sha="abc1234").get("/healthz")
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "git_sha": "abc1234", "db": "ok"}
    down = client(FakeReader({}, fail=True), evcrawl_git_sha="abc1234").get("/healthz")
    assert down.status_code == 503
    assert down.json() == {"ok": False, "git_sha": "abc1234", "db": "unavailable"}


def test_healthz_without_a_database_url_is_unavailable_not_a_fallback() -> None:
    response = client(ApiDatabase(None)).get("/healthz")
    assert response.status_code == 503 and response.json()["db"] == "unavailable"


def test_categories_are_the_view_rows_cached_for_fifteen_minutes(fake: FakeReader) -> None:
    response = client(fake).get("/v1/categories")
    assert response.status_code == 200
    assert response.json() == {"data": CATEGORIES}
    assert response.headers["cache-control"] == "public, max-age=900"
    assert response.headers["etag"].startswith('W/"')
    assert "api_categories" in fake.sql[-1]


def test_cities_come_from_the_registry_with_counts_from_the_view(fake: FakeReader) -> None:
    data = client(fake).get("/v1/cities").json()["data"]
    assert [c["slug"] for c in data] == [c.slug for c in cities.CITIES if c.enabled]
    by_slug = {c["slug"]: c for c in data}
    assert by_slug["vitoria"] == {
        "slug": "vitoria",
        "name": "Vitória",
        "uf": "ES",
        "timezone": "America/Sao_Paulo",
        "events_upcoming": 214,
    }
    assert by_slug["manaus"]["events_upcoming"] == 0
    assert by_slug["manaus"]["timezone"] == "America/Manaus"


def test_matching_if_none_match_is_a_304_without_a_body(fake: FakeReader) -> None:
    c = client(fake)
    etag = c.get("/v1/cities").headers["etag"]
    again = c.get("/v1/cities", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag
    assert c.get("/v1/cities", headers={"If-None-Match": 'W/"other"'}).status_code == 200


def test_errors_use_the_envelope(fake: FakeReader) -> None:
    c = client(fake)
    missing = c.get("/v1/nope")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "Not Found"}}
    post = c.post("/v1/categories")
    assert post.status_code == 405 and post.json()["error"]["code"] == "method_not_allowed"
    down = client(FakeReader({}, fail=True)).get("/v1/categories")
    assert down.status_code == 503 and down.json()["error"]["code"] == "db_unavailable"


def test_cors_is_an_allowlist_of_get_without_credentials(fake: FakeReader) -> None:
    preflight = {"Origin": "https://pwa.example.org", "Access-Control-Request-Method": "GET"}
    closed = client(fake).options("/v1/cities", headers=preflight)
    assert "access-control-allow-origin" not in closed.headers
    c = client(fake, evcrawl_api_cors_origins="https://pwa.example.org, https://b.example.org")
    opened = c.options("/v1/cities", headers=preflight)
    assert opened.headers["access-control-allow-origin"] == "https://pwa.example.org"
    assert "access-control-allow-credentials" not in opened.headers
    assert opened.headers["access-control-allow-methods"] == "GET"
    other = c.get("/v1/cities", headers={"Origin": "https://evil.example.org"})
    assert "access-control-allow-origin" not in other.headers


def test_one_log_line_per_request_without_query_or_address(
    fake: FakeReader, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")  # the JSON lines `evcrawl api` writes
    client(fake).get("/v1/cities?lat=-20.3155&lon=-40.3128", headers={"X-Forwarded-For": "1.2.3.4"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "api." in line]
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "api.request"
    assert (line["method"], line["route"], line["status"]) == ("GET", "/v1/cities", 200)
    assert line["rows"] == len([c for c in cities.CITIES if c.enabled])
    text_line = json.dumps(line)
    for leaked in ("-20.3155", "lat=", "1.2.3.4", "testclient"):
        assert leaked not in text_line


def test_committed_openapi_is_the_apps(fake: FakeReader) -> None:
    """``docs/api/openapi.json`` is the PWA's contract: regenerate it with
    ``uv run evcrawl api --openapi > docs/api/openapi.json`` and commit the diff."""
    assert OPENAPI.read_text() == openapi_json()
    paths = json.loads(OPENAPI.read_text())["paths"]
    assert set(paths) == {"/healthz", "/v1/categories", "/v1/cities"}
    assert all(set(ops) == {"get"} for ops in paths.values())


def test_sessions_are_read_only_with_a_timeout() -> None:
    assert "default_transaction_read_only=on" in SESSION_OPTIONS
    assert "statement_timeout=3s" in SESSION_OPTIONS


# --- against the scratch database, as api_ro


@pytest.fixture
def api_ro_db(scratch_engine: Engine) -> Iterator[ApiDatabase]:
    """``ApiDatabase`` whose connections ``set role api_ro``, with the API's session options."""
    with scratch_engine.connect() as conn:
        if role_can_login(conn, API_ROLE) is None:
            pytest.skip("api_ro missing: the shared db is not released with 0008 yet")
    assert SCRATCH_URL is not None
    engine = create_engine(SCRATCH_URL, connect_args={"options": SESSION_OPTIONS})

    @event.listens_for(engine, "connect")
    def _as_api_ro(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.autocommit = True
        dbapi_conn.execute(f"set role {API_ROLE}")
        dbapi_conn.autocommit = False

    db = ApiDatabase(engine=engine)
    try:
        yield db
    finally:
        db.dispose()


def test_api_serves_the_seeded_catalogue_as_api_ro(
    scratch_engine: Engine, api_ro_db: ApiDatabase
) -> None:
    _seed(scratch_engine)
    c = client(api_ro_db, evcrawl_git_sha="abc1234")
    assert c.get("/healthz").json() == {"ok": True, "git_sha": "abc1234", "db": "ok"}
    assert c.get("/v1/categories").json()["data"] == [
        {"slug": "music", "name": "Música", "events_upcoming": 2},
        {"slug": "uncategorized", "name": "Sem categoria", "events_upcoming": 1},
    ]
    counts = {c["slug"]: c["events_upcoming"] for c in c.get("/v1/cities").json()["data"]}
    assert (counts["vitoria"], counts["vila-velha"], counts["sao-paulo"]) == (2, 1, 1)
    assert counts["manaus"] == 0


def test_api_sessions_cannot_write_even_where_the_role_could(api_ro_db: ApiDatabase) -> None:
    """``default_transaction_read_only`` from the connection options, whatever the grants."""
    with api_ro_db.engine.connect() as conn:
        assert conn.execute(text("show transaction_read_only")).scalar_one() == "on"
        assert conn.execute(text("show statement_timeout")).scalar_one() == "3s"
        with pytest.raises(DBAPIError, match="read-only transaction"):
            conn.execute(text("create temporary table t (x int)"))
