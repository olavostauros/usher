"""Crawl runner and CLI wiring with a fake adapter; full flow on the scratch db."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from evcrawl.cities import City, resolve
from evcrawl.cli import app
from evcrawl.config import Settings
from evcrawl.db.models import (
    CrawlKind,
    CrawlRun,
    Event,
    EventStatus,
    EventVersion,
    SourceListing,
    Venue,
)
from evcrawl.pipeline import crawl as crawl_module
from evcrawl.pipeline.crawl import CrawlSummary, run_crawl, run_refresh, upcoming_listing_urls
from evcrawl.pipeline.fetch import Fetcher, FetchError, FetchText
from evcrawl.sources import REGISTRY
from evcrawl.sources.base import Discovered, EventRecord, PriceLot, RateLimit, SourceAdapter

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
    fingerprints: dict[str, str] = {}  # url -> listing fingerprint; yields Discovered when set
    failing: set[str] = set()
    raises: dict[str, FetchError] = {}  # url -> the error its fetch raises
    discover_calls: list[City | None] = []
    fetches: list[str] = []

    def __init__(self, fetch: FetchText | None = None) -> None:
        pass

    async def discover(self, city: City | None = None) -> AsyncIterator[str | Discovered]:
        self.discover_calls.append(city)
        for url, record in self.records.items():
            fingerprint = self.fingerprints.get(url)
            yield url if fingerprint is None else Discovered(url, record.external_id, fingerprint)

    async def fetch(self, external_id: str) -> EventRecord:
        self.fetches.append(external_id)
        if external_id in self.failing:
            raise FetchError(external_id, 500)
        if external_id in self.raises:
            raise self.raises[external_id]
        return self.records[external_id]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeAdapter]:
    FakeAdapter.records = {str(r.source_url): r for r in (make_record(i) for i in range(1, 4))}
    FakeAdapter.fingerprints = {}
    FakeAdapter.failing = set()
    FakeAdapter.raises = {}
    FakeAdapter.discover_calls = []
    FakeAdapter.fetches = []
    monkeypatch.setitem(REGISTRY, "fake", FakeAdapter)
    return FakeAdapter


def fingerprinted(fake: type[FakeAdapter], overrides: dict[str, str] | None = None) -> None:
    """Make discover() yield ``Discovered`` for every record (ADR 0013)."""
    fake.fingerprints = {url: f"fp:{url}" for url in fake.records} | (overrides or {})


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
        "fake", city=resolve("vitoria"), dry_run=True, limit=2, fetcher=offline_fetcher()
    )
    assert summary.discovered == 2 and summary.fetched == 2
    assert summary.city == "vitoria"
    assert fake.discover_calls == [resolve("vitoria")]


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


def gone(fake: type[FakeAdapter], *ids: int, status: int = 404) -> None:
    """The source answers *status* for the event page of each of *ids* (ADR 0022)."""
    for i in ids:
        url = f"https://fake.example/e/{i}"
        fake.raises[url] = FetchError(url, status)


async def test_a_gone_page_is_counted_gone_not_as_an_error(fake: type[FakeAdapter]) -> None:
    gone(fake, 2)
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert (summary.fetched, summary.gone, summary.errors) == (2, 1, 0)
    assert summary.error_detail is None and not summary.failed


async def test_gone_pages_are_errors_when_they_outnumber_the_fetched(
    fake: type[FakeAdapter],
) -> None:
    gone(fake, 2, 3, status=410)  # a source that moved its URLs must not empty the catalogue
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert (summary.fetched, summary.gone, summary.errors) == (1, 0, 2)


async def test_a_404_on_another_url_is_an_error(fake: type[FakeAdapter]) -> None:
    fake.raises["https://fake.example/e/2"] = FetchError("https://fake.example/api/2", 404)
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert (summary.gone, summary.errors) == (0, 1)


@pytest.mark.parametrize(
    ("discovered", "fetched", "skipped", "errors", "failed"),
    [
        (10, 0, 10, 0, False),  # every listing unchanged: healthy
        (10, 0, 9, 1, True),  # the rest failed: skips no longer hide it
        (10, 0, 0, 1, True),
        (10, 1, 8, 1, False),  # one page fetched: the source works
        (0, 0, 0, 1, False),  # nothing discovered: not this rule
    ],
)
def test_failed_rule(
    discovered: int, fetched: int, skipped: int, errors: int, failed: bool
) -> None:
    summary = CrawlSummary(
        source="fake",
        city=None,
        kind=CrawlKind.MANUAL,
        dry_run=True,
        limit=None,
        started_at=datetime.now(UTC),
        discovered=discovered,
        fetched=fetched,
        skipped=skipped,
        errors=errors,
    )
    assert summary.failed is failed


async def test_run_kind_defaults_to_crawl(fake: type[FakeAdapter]) -> None:
    summary = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert summary.kind is CrawlKind.CRAWL  # what the scheduler's cycles write


async def test_an_injected_fetcher_outlives_the_run(fake: type[FakeAdapter]) -> None:
    """The scheduler shares one Fetcher between every job; a run must not close it."""
    fetcher = offline_fetcher()
    first = await run_crawl("fake", dry_run=True, fetcher=fetcher)
    second = await run_crawl("fake", dry_run=True, fetcher=fetcher)
    assert (first.fetched, second.fetched) == (3, 3)
    assert not fetcher._client.is_closed
    await fetcher.aclose()


def test_cli_crawls_are_manual(fake: type[FakeAdapter], monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-run crawl must not count as the coverage freshness is measured on."""
    kinds: list[CrawlKind] = []
    real = crawl_module.run_crawl

    async def spy(source: str, **kwargs: Any) -> Any:
        kinds.append(kwargs["kind"])
        return await real(source, **kwargs)

    monkeypatch.setattr(crawl_module, "Fetcher", lambda *a, **k: offline_fetcher())
    monkeypatch.setattr(crawl_module, "run_crawl", spy)
    assert CliRunner().invoke(app, ["crawl", "fake", "--dry-run"]).exit_code == 0
    assert kinds == [CrawlKind.MANUAL]


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
    return sessionmaker(bind=scratch_engine, expire_on_commit=False)


async def test_crawl_writes_events_and_a_run_row_and_is_idempotent(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    first = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (first.discovered, first.fetched, first.upserted, first.created) == (3, 3, 3, 3)
    assert first.versions == 3 and first.errors == 0 and first.removed == 0

    second = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (second.upserted, second.created, second.versions) == (3, 0, 0)
    assert second.skipped == 0  # plain URLs are always fetched

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
        # every count the summary keeps is on the row
        assert (runs[0].created, runs[0].versions) == (3, 3)
        assert (runs[0].merged, runs[0].removed, runs[0].review, runs[0].skipped) == (0, 0, 0, 0)
        assert (runs[1].created, runs[1].versions) == (0, 0)
        # no --city: the run covered every city the source lists
        assert runs[0].city is None and runs[0].kind is CrawlKind.CRAWL


async def test_second_source_merges_into_the_first_and_counts_it(
    fake: type[FakeAdapter], factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    class OtherAdapter(FakeAdapter):
        name = "other"
        base_url = "https://other.example"
        records = {
            "https://other.example/e/1": make_record(
                1, source="other", source_url="https://other.example/e/1", description="x"
            ),
            "https://other.example/e/9": make_record(
                9, source="other", source_url="https://other.example/e/9"
            ),
        }
        discover_calls = []

    monkeypatch.setitem(REGISTRY, "other", OtherAdapter)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    other = await run_crawl("other", session_factory=factory, fetcher=offline_fetcher())
    assert (other.upserted, other.created, other.merged, other.versions) == (2, 1, 1, 2)
    assert other.review == 0
    with factory() as session:
        assert session.scalar(select(func.count(Event.id))) == 4
        merged = session.scalars(select(Event).where(Event.title == "Evento 1")).one()
        assert sorted(listing.source.name for listing in merged.listings) == ["fake", "other"]
        assert merged.description == "x"  # filled from the second source
        assert not merged.dedupe_review

    again = await run_crawl("other", session_factory=factory, fetcher=offline_fetcher())
    assert (again.created, again.merged, again.versions, again.review) == (0, 0, 0, 0)


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


async def test_city_crawl_without_limit_marks_that_city_missing(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    vitoria, serra = resolve("vitoria"), resolve("serra")
    await run_crawl("fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher())
    del fake.records["https://fake.example/e/3"]
    limited = await run_crawl(
        "fake", city=vitoria, limit=5, session_factory=factory, fetcher=offline_fetcher()
    )
    assert limited.removed == 0
    # another city's complete crawl does not touch Vitória's listings
    other = await run_crawl("fake", city=serra, session_factory=factory, fetcher=offline_fetcher())
    assert other.removed == 0
    with factory() as session:
        cities = sorted(c for c in session.scalars(select(SourceListing.city)).all() if c)
        assert cities == ["serra", "serra", "vitoria"]  # e/1 and e/2 were seen under Serra last

    fake.records["https://fake.example/e/3"] = make_record(3)
    await run_crawl("fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher())
    del fake.records["https://fake.example/e/3"]
    full = await run_crawl("fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher())
    assert full.removed == 1
    with factory() as session:
        run = session.get_one(CrawlRun, full.crawl_run_id)
        assert run.removed == 1
        listing = session.scalars(
            select(SourceListing).where(SourceListing.external_id == "3")
        ).one()
        assert listing.removed_at is not None
        assert listing.event.status is EventStatus.REMOVED_FROM_SOURCE


async def test_run_row_is_tagged_with_the_city_it_covered(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    summary = await run_crawl(
        "fake", city=resolve("vitoria"), session_factory=factory, fetcher=offline_fetcher()
    )
    with factory() as session:
        run = session.get(CrawlRun, summary.crawl_run_id)
        assert run is not None and run.city == "vitoria" and run.kind is CrawlKind.CRAWL


async def test_refresh_run_row_is_kind_refresh(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    """Spec 3's SLAs read the two kinds separately; a refresh is not a cycle."""
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    summary = await run_refresh("fake", session_factory=factory, fetcher=offline_fetcher())
    assert summary.kind is CrawlKind.REFRESH
    with factory() as session:
        run = session.get(CrawlRun, summary.crawl_run_id)
        assert run is not None and run.kind is CrawlKind.REFRESH and run.city is None


async def test_run_row_records_merged_listings(
    fake: type[FakeAdapter], factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    class OtherAdapter(FakeAdapter):
        name = "other"
        base_url = "https://other.example"
        records = {
            "https://other.example/e/1": make_record(
                1, source="other", source_url="https://other.example/e/1"
            )
        }
        discover_calls = []

    monkeypatch.setitem(REGISTRY, "other", OtherAdapter)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    summary = await run_crawl("other", session_factory=factory, fetcher=offline_fetcher())
    with factory() as session:
        run = session.get(CrawlRun, summary.crawl_run_id)
        assert run is not None and (run.merged, run.created) == (1, 0)


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


async def test_refresh_removes_a_listing_whose_page_is_gone(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    soon = datetime.now(UTC) + timedelta(hours=5)
    fake.records = {
        str(r.source_url): r for r in (make_record(i, starts_at=soon) for i in range(1, 4))
    }
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    gone(fake, 1)
    summary = await run_refresh("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (summary.fetched, summary.gone, summary.removed, summary.errors) == (2, 1, 1, 0)
    with factory() as session:
        event = session.scalars(select(Event).where(Event.title == "Evento 1")).one()
        assert event.status is EventStatus.REMOVED_FROM_SOURCE
        assert event.listings[0].removed_at is not None
        last = max(event.versions, key=lambda v: v.version)
        assert last.content["reason"] == "source page gone"
        run = session.get(CrawlRun, summary.crawl_run_id)
        assert run is not None and run.errors == 0 and run.removed == 1
    again = await run_refresh("fake", session_factory=factory, fetcher=offline_fetcher())
    assert again.discovered == 2 and again.errors == 0  # a removed listing is not retried


async def test_a_city_crawl_with_a_gone_page_still_sweeps_the_city(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    vitoria = resolve("vitoria")
    fake.records |= {str(r.source_url): r for r in (make_record(i) for i in (4, 5))}
    await run_crawl("fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher())
    gone(fake, 2)  # listed, but its page is gone
    del fake.records["https://fake.example/e/3"]  # no longer listed
    summary = await run_crawl(
        "fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher()
    )
    assert (summary.gone, summary.errors, summary.removed) == (1, 0, 2)
    with factory() as session:
        removed = session.scalars(
            select(Event.title).where(Event.status == EventStatus.REMOVED_FROM_SOURCE)
        ).all()
        assert sorted(removed) == ["Evento 2", "Evento 3"]


async def test_refresh_leaves_listing_only_urls_to_the_cycle(
    fake: type[FakeAdapter], factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    soon = datetime.now(UTC) + timedelta(hours=5)
    fake.records = {
        str(r.source_url): r for r in (make_record(i, starts_at=soon) for i in range(1, 4))
    }
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    monkeypatch.setattr(
        FakeAdapter, "refetchable", classmethod(lambda cls, url: not url.endswith("/2"))
    )
    fake.fetches = []
    summary = await run_refresh("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (summary.discovered, summary.fetched, summary.errors) == (2, 2, 0)
    assert "https://fake.example/e/2" not in fake.fetches


# --- conditional fetch (ADR 0013)

URL = {i: f"https://fake.example/e/{i}" for i in (1, 2, 3)}


async def test_second_run_skips_every_unchanged_listing(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    fingerprinted(fake)
    first = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (first.fetched, first.skipped, first.upserted) == (3, 0, 3)
    assert len(fake.fetches) == 3

    second = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (second.discovered, second.fetched, second.skipped, second.upserted) == (3, 0, 3, 0)
    assert second.errors == 0 and not second.failed
    assert len(fake.fetches) == 3  # no event page was fetched
    with factory() as session:
        run = session.get_one(CrawlRun, second.crawl_run_id)
        assert (run.discovered, run.fetched, run.skipped, run.upserted) == (3, 0, 3, 0)
        listings = session.scalars(select(SourceListing).order_by(SourceListing.external_id)).all()
        assert [x.listing_fingerprint for x in listings] == [f"fp:{URL[i]}" for i in (1, 2, 3)]
        assert all(x.last_crawl_run_id == second.crawl_run_id for x in listings)
        assert all(x.last_seen_at >= first.finished_at for x in listings)
        assert session.scalar(select(func.count(EventVersion.id))) == 3  # skips write nothing


async def test_a_changed_listing_item_is_fetched_again(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    fingerprinted(fake)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    fake.records[URL[2]] = make_record(2, title="Evento 2 (novo)")
    fingerprinted(fake, {URL[2]: "fp:v2"})

    second = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (second.fetched, second.skipped, second.upserted, second.versions) == (1, 2, 1, 1)
    assert fake.fetches[3:] == [URL[2]]
    with factory() as session:
        listing = session.scalars(
            select(SourceListing).where(SourceListing.external_id == "2")
        ).one()
        assert listing.listing_fingerprint == "fp:v2"
        assert listing.event.title == "Evento 2 (novo)"


async def test_an_event_within_48h_is_always_fetched(
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
    fingerprinted(fake)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    second = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (second.fetched, second.skipped) == (2, 1)
    assert fake.fetches[3:] == [URL[1], URL[2]]


async def test_a_skipped_listing_is_seen_and_an_all_skipped_run_still_sweeps(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    vitoria = resolve("vitoria")
    fingerprinted(fake)
    await run_crawl("fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher())
    del fake.records[URL[3]], fake.fingerprints[URL[3]]

    second = await run_crawl(
        "fake", city=vitoria, session_factory=factory, fetcher=offline_fetcher()
    )
    assert (second.fetched, second.skipped, second.removed) == (0, 2, 1)
    with factory() as session:
        removed_at = dict(
            session.execute(select(SourceListing.external_id, SourceListing.removed_at)).all()
        )
        assert removed_at["1"] is None and removed_at["2"] is None
        assert removed_at["3"] is not None
        assert session.get_one(CrawlRun, second.crawl_run_id).removed == 1


async def test_a_removed_listing_seen_again_is_fetched_and_revived(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    fingerprinted(fake)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    record, fingerprint = fake.records.pop(URL[3]), fake.fingerprints.pop(URL[3])
    gone = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert gone.removed == 1
    fake.records[URL[3]], fake.fingerprints[URL[3]] = (
        record,
        fingerprint,
    )  # same item, same fingerprint

    back = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (back.fetched, back.skipped, back.versions) == (1, 2, 1)
    with factory() as session:
        listing = session.scalars(
            select(SourceListing).where(SourceListing.external_id == "3")
        ).one()
        assert listing.removed_at is None and listing.event.status is EventStatus.SCHEDULED


async def test_refresh_and_plain_urls_leave_fingerprints_in_place(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    soon = datetime.now(UTC) + timedelta(hours=5)
    fake.records = {str(r.source_url): r for r in (make_record(1, starts_at=soon), make_record(2))}
    fingerprinted(fake)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    refresh = await run_refresh("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (refresh.fetched, refresh.skipped) == (1, 0)
    fake.fingerprints = {}  # an adapter yielding plain URLs
    plain = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (plain.fetched, plain.skipped, plain.upserted) == (2, 0, 2)
    with factory() as session:
        stored = dict(
            session.execute(
                select(SourceListing.external_id, SourceListing.listing_fingerprint)
            ).all()
        )
        assert stored == {"1": f"fp:{URL[1]}", "2": f"fp:{URL[2]}"}


async def test_dry_run_has_nothing_to_compare_and_fetches(
    fake: type[FakeAdapter], factory: sessionmaker[Session]
) -> None:
    fingerprinted(fake)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    dry = await run_crawl("fake", dry_run=True, fetcher=offline_fetcher())
    assert (dry.fetched, dry.skipped, dry.upserted) == (3, 0, 0)


async def test_a_failing_skip_check_falls_back_to_fetching(
    fake: type[FakeAdapter], factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprinted(fake)
    await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("db gone")

    monkeypatch.setattr(crawl_module, "skip_unchanged", boom)
    second = await run_crawl("fake", session_factory=factory, fetcher=offline_fetcher())
    assert (second.fetched, second.skipped, second.upserted, second.errors) == (3, 0, 3, 3)
    assert second.removed == 0  # errors: not a complete run
    assert second.error_detail is not None and f"skip {URL[1]}: RuntimeError" in second.error_detail
