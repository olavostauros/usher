"""Crawl runner and CLI wiring with a fake adapter; full flow on the scratch db."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from evcrawl.cli import app
from evcrawl.config import Settings
from evcrawl.db.models import CrawlRun, Event, EventVersion, Venue
from evcrawl.pipeline import crawl as crawl_module
from evcrawl.pipeline.crawl import run_crawl, run_refresh, upcoming_listing_urls
from evcrawl.pipeline.fetch import Fetcher, FetchError, FetchText
from evcrawl.sources import REGISTRY
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

START = datetime.now(UTC) + timedelta(days=10)


def make_record(i: int, **overrides: Any) -> EventRecord:
    base: dict[str, Any] = {
        "source": "fake",
        "external_id": str(i),
        "source_url": f"https://fake.example/e/{i}",
        "title": f"Evento {i}",
        "starts_at": START + timedelta(days=i),
        "venue_name": f"Local {i}",
        "address": f"Rua {i}, {i}, Centro, Vitória - ES",
        "city": "Vitória",
        "state": "ES",
        "latitude": -20.31 + i / 1000,
        "longitude": -40.30,
        "organizer": "Org",
        "category": "musica",
        "price_lots": [PriceLot(name="Gratuito", amount=Decimal(0))],
        "raw": {"i": i},
    }
    base.update(overrides)
    return EventRecord(**base)


class FakeAdapter(SourceAdapter):
    name = "fake"
    base_url = "https://fake.example"
    rate_limit = RateLimit(requests_per_second=100)
    records: dict[str, EventRecord] = {}
    failing: set[str] = set()
    discover_calls: list[str | None] = []

    def __init__(self, fetch: FetchText | None = None) -> None:
        pass

    async def discover(self, city: str | None = None) -> AsyncIterator[str]:
        self.discover_calls.append(city)
        for url in self.records:
            yield url

    async def fetch(self, external_id: str) -> EventRecord:
        if external_id in self.failing:
            raise FetchError(external_id, 500)
        return self.records[external_id]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeAdapter]:
    FakeAdapter.records = {str(r.source_url): r for r in (make_record(i) for i in range(1, 4))}
    FakeAdapter.failing = set()
    FakeAdapter.discover_calls = []
    monkeypatch.setitem(REGISTRY, "fake", FakeAdapter)
    return FakeAdapter


def offline_fetcher() -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    return Fetcher(Settings(), transport=httpx.MockTransport(handler))


# --- dry run, offline


async def test_dry_run_parses_everything_and_writes_nothing(fake: type[FakeAdapter]) -> None:
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert (summary.discovered, summary.fetched, summary.upserted) == (3, 3, 0)
    assert summary.crawl_run_id is None and summary.errors == 0 and not summary.failed
    assert fake.discover_calls == [None]


async def test_limit_stops_discovery(fake: type[FakeAdapter]) -> None:
    summary = await run_crawl(
        "fake", city="vitoria", dry_run=True, limit=2, fetcher=offline_fetcher()
    )
    assert summary.discovered == 2 and summary.fetched == 2
    assert fake.discover_calls == ["vitoria"]


async def test_fetch_failures_are_counted_not_fatal(fake: type[FakeAdapter]) -> None:
    fake.failing = {"https://fake.example/e/2"}
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert (summary.discovered, summary.fetched, summary.errors) == (3, 2, 1)
    assert (
        summary.error_detail is not None
        and "fetch https://fake.example/e/2" in summary.error_detail
    )
    assert not summary.failed


async def test_all_fetches_failing_means_failed(fake: type[FakeAdapter]) -> None:
    fake.failing = set(fake.records)
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert summary.failed


def test_cli_exit_codes(fake: type[FakeAdapter], monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(crawl_module, "Fetcher", lambda *a, **k: offline_fetcher())
    assert runner.invoke(app, ["crawl", "fake", "--dry-run"]).exit_code == 0
    fake.failing = set(fake.records)
    assert runner.invoke(app, ["crawl", "fake", "--dry-run"]).exit_code == 1
    assert runner.invoke(app, ["crawl", "nope", "--dry-run"]).exit_code == 2


# --- full flow on the scratch database


@pytest.fixture
def factory(scratch_engine: Engine) -> sessionmaker[Session]:
    with Session(scratch_engine) as s:
        for table in (
            "raw_snapshots",
            "price_lots",
            "event_versions",
            "source_listings",
            "events",
            "crawl_runs",
        ):
            s.execute(text(f"delete from {table}"))  # scratch db only (conftest)
        s.commit()
    return sessionmaker(bind=scratch_engine, expire_on_commit=False)


async def test_crawl_writes_events_and_a_run_row_and_is_idempotent(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    first = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (first.discovered, first.fetched, first.upserted, first.created) == (3, 3, 3, 3)
    assert first.versions == 3 and first.errors == 0 and first.removed == 0

    second = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (second.upserted, second.created, second.versions) == (3, 0, 0)

    with factory() as session:
        assert session.scalar(select(func.count(Event.id))) == 3
        assert session.scalar(select(func.count(EventVersion.id))) == 3
        assert session.scalar(select(func.count(Venue.id)).where(Venue.location.is_not(None))) == 3
        runs = session.scalars(select(CrawlRun).order_by(CrawlRun.id)).all()
        assert [r.id for r in runs] == [first.crawl_run_id, second.crawl_run_id]
        assert (runs[0].discovered, runs[0].fetched, runs[0].upserted, runs[0].errors) == (
            3,
            3,
            3,
            0,
        )
        assert runs[0].finished_at is not None and runs[0].error_detail is None


async def test_full_crawl_marks_missing_but_partial_does_not(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    del fake.records["https://fake.example/e/3"]
    partial = await run_crawl("fake", limit=2, session_factory=factory, fetcher=offline_fetcher())
    assert partial.removed == 0
    full = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert full.removed == 1
    with factory() as session:
        statuses = sorted(s.value for s in session.scalars(select(Event.status)).all())
        assert statuses == ["removed_from_source", "scheduled", "scheduled"]


async def test_run_row_records_errors(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    fake.failing = {"https://fake.example/e/1"}
    summary = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    with factory() as session:
        run = session.get(CrawlRun, summary.crawl_run_id)
        assert run is not None and run.errors == 1 and run.fetched == 2
        assert run.error_detail is not None and "FetchError" in run.error_detail


async def test_refresh_refetches_only_events_starting_within_48h(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    soon = datetime.now(UTC) + timedelta(hours=5)
    fake.records = {
        str(r.source_url): r
        for r in (
            make_record(1, starts_at=soon),
            make_record(2, starts_at=soon + timedelta(hours=40)),
            make_record(3, starts_at=soon + timedelta(days=10)),
        )
    }
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    with factory() as session:
        urls = upcoming_listing_urls(session, "fake")
    assert urls == ["https://fake.example/e/1", "https://fake.example/e/2"]

    fake.records["https://fake.example/e/1"] = make_record(
        1, starts_at=soon, title="Evento 1 (novo)"
    )
    del fake.records["https://fake.example/e/3"]  # a refresh never discovers, never removes
    summary = await run_refresh("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (summary.discovered, summary.fetched, summary.upserted) == (2, 2, 2)
    assert summary.versions == 1 and summary.removed == 0
    with factory() as session:
        titles = sorted(session.scalars(select(Event.title)).all())
        assert titles == ["Evento 1 (novo)", "Evento 2", "Evento 3"]
