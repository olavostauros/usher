"""build_scheduler() registers the standing jobs; run_cycle() walks a source's cities.

Nothing is started here and nothing reaches the network: fixtures serve the
Zig feed and a mock transport answers 404 to everything else (missing
``robots.txt`` allows all; a failing geocoder leaves the venue as it is). The
heartbeat's write is exercised against the scratch database (skipped without
``EVCRAWL_TEST_DATABASE_URL``).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from evcrawl.cities import CITIES, City, for_source, resolve
from evcrawl.config import Settings
from evcrawl.db.models import CrawlKind, SchedulerHeartbeat
from evcrawl.pipeline.crawl import run_cycle, run_refresh
from evcrawl.pipeline.fetch import Fetcher, FetchText
from evcrawl.scheduler import (
    CRAWL_EVERY_HOURS,
    HEARTBEAT_JOB_ID,
    HEARTBEAT_SECONDS,
    beat,
    build_scheduler,
    cycle_cities,
)
from evcrawl.sources import REGISTRY
from evcrawl.sources.base import EventRecord, RateLimit, SourceAdapter
from evcrawl.sources.zig import FEED_URL, ZigAdapter
from tests.test_sources_zig import FakeFetch, routes

SOURCES = sorted(REGISTRY)


def offline_fetcher() -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    return Fetcher(Settings(), transport=httpx.MockTransport(handler))


# --- the registered jobs


def test_cycle_cities_is_the_registry_per_source() -> None:
    cycles = cycle_cities()
    assert set(cycles) == set(REGISTRY)
    for source, slugs in cycles.items():
        assert slugs == [c.slug for c in for_source(source)]
        assert slugs == [c.slug for c in CITIES]  # the capitals are on since task 8


def test_build_scheduler_registers_one_cycle_and_one_refresh_per_source() -> None:
    sched = build_scheduler(Settings(), offline_fetcher())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert len(jobs) == 2 * len(REGISTRY) + 1  # + the heartbeat
    expected = [f"cycle:{s}" for s in REGISTRY] + [f"refresh:{s}" for s in REGISTRY]
    assert sorted(jobs) == sorted([*expected, HEARTBEAT_JOB_ID])
    for source in REGISTRY:
        cycle = jobs[f"cycle:{source}"]
        assert cycle.func is run_cycle and cycle.args == (source,)
        assert "city" not in cycle.kwargs and "cities" not in cycle.kwargs  # the registry decides
        assert cycle.max_instances == 1 and cycle.coalesce
        refresh = jobs[f"refresh:{source}"]
        assert refresh.func is run_refresh and refresh.args == (source,)
        assert refresh.max_instances == 1 and refresh.coalesce


def test_cycle_and_refresh_share_the_process_fetcher() -> None:
    fetcher = offline_fetcher()
    sched = build_scheduler(Settings(), fetcher)
    crawl_jobs = [job for job in sched.get_jobs() if job.id != HEARTBEAT_JOB_ID]
    assert len(crawl_jobs) == 2 * len(REGISTRY)
    for job in crawl_jobs:
        assert job.kwargs["fetcher"] is fetcher


# --- the heartbeat (docs/specs/phase-3-monitoring.md §3)


def test_heartbeat_job_beats_every_minute_starting_now() -> None:
    before = datetime.now(UTC)
    sched = build_scheduler(Settings(), offline_fetcher())
    job = sched.get_job(HEARTBEAT_JOB_ID)
    assert job.func is beat and job.kwargs["sched"] is sched
    assert job.max_instances == 1 and job.coalesce
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(seconds=HEARTBEAT_SECONDS)
    assert HEARTBEAT_SECONDS == 60
    # The first beat is due at start (not a minute later), the next one an interval on.
    assert before <= job.next_run_time <= datetime.now(UTC)
    assert before <= job.kwargs["started_at"] <= datetime.now(UTC)


def test_beat_upserts_the_one_row_and_v_scheduler_reads_it(scratch_engine: Engine) -> None:
    factory = sessionmaker(bind=scratch_engine)
    settings = Settings(evcrawl_git_sha="abc1234")
    sched = build_scheduler(settings, offline_fetcher(), session_factory=factory)
    started = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    beat(sched, settings, started, session_factory=factory)
    beat(sched, settings, started, session_factory=factory)  # a second beat updates, never inserts

    with factory() as session:
        rows = session.query(SchedulerHeartbeat).all()
        assert len(rows) == 1 and rows[0].id == 1
        row = rows[0]
        assert row.started_at == started and row.git_sha == "abc1234"
        assert row.jobs == len(sched.get_jobs()) == 2 * len(REGISTRY) + 1
        assert datetime.now(UTC) - row.beat_at < timedelta(seconds=30)
    with scratch_engine.connect() as conn:
        age, jobs, sha = conn.execute(
            text("select age_seconds, jobs, git_sha from v_scheduler")
        ).one()
        assert 0 <= age < 30 and jobs == row.jobs and sha == "abc1234"

    # A new scheduler process takes the row over: its own start, its own build.
    later = started + timedelta(hours=1)
    beat(sched, Settings(evcrawl_git_sha="def5678"), later, session_factory=factory)
    with factory() as session:
        row = session.query(SchedulerHeartbeat).one()
        assert row.started_at == later and row.git_sha == "def5678"


def test_beat_survives_a_database_error(scratch_engine: Engine) -> None:
    """A failed beat is a log line, not an exception: the crawl jobs must keep running."""
    broken = sessionmaker(bind=scratch_engine)
    with scratch_engine.begin() as conn:
        conn.execute(text("alter table scheduler_heartbeat rename to scheduler_heartbeat_gone"))
    try:
        beat(build_scheduler(Settings(), offline_fetcher()), Settings(), datetime.now(UTC), broken)
    finally:
        with scratch_engine.begin() as conn:
            conn.execute(text("alter table scheduler_heartbeat_gone rename to scheduler_heartbeat"))


def fire_times(trigger: CronTrigger, start: datetime, count: int) -> list[datetime]:
    out: list[datetime] = []
    previous: datetime | None = None
    now = start
    while len(out) < count:
        nxt = trigger.get_next_fire_time(previous, now)
        assert nxt is not None
        out.append(nxt)
        previous = nxt
        now = nxt
    return out


@pytest.mark.parametrize("source", SOURCES)
def test_cycles_run_every_six_hours(source: str) -> None:
    sched = build_scheduler(Settings(), offline_fetcher())
    trigger = sched.get_job(f"cycle:{source}").trigger
    assert isinstance(trigger, CronTrigger)
    times = fire_times(trigger, datetime(2026, 9, 21, 0, 0, tzinfo=UTC), 3)
    assert [(b - a).total_seconds() for a, b in pairwise(times)] == [CRAWL_EVERY_HOURS * 3600] * 2


@pytest.mark.parametrize("source", SOURCES)
def test_refresh_runs_hourly(source: str) -> None:
    sched = build_scheduler(Settings(), offline_fetcher())
    trigger = sched.get_job(f"refresh:{source}").trigger
    assert isinstance(trigger, CronTrigger)
    times = fire_times(trigger, datetime(2026, 9, 21, 0, 0, tzinfo=UTC), 3)
    assert [(b - a).total_seconds() for a, b in pairwise(times)] == [3600, 3600]
    assert all(t.minute != 0 for t in times)  # never on the hour, when the cycles start


# --- one cycle


async def test_cycle_fetches_the_zig_feed_once_for_two_cities() -> None:
    fake = FakeFetch(routes())
    cities = [resolve("vila-velha"), resolve("vitoria")]
    summaries = await run_cycle(
        "zig", cities=cities, dry_run=True, fetcher=offline_fetcher(), adapter=ZigAdapter(fake)
    )
    assert fake.calls.count(FEED_URL) == 1
    assert [s.city for s in summaries] == ["vila-velha", "vitoria"]
    assert all(s.kind is CrawlKind.CRAWL and s.dry_run for s in summaries)
    assert sum(s.discovered for s in summaries) > 0


class CountingAdapter(SourceAdapter):
    name = "counting"
    base_url = "https://counting.example"
    rate_limit = RateLimit(requests_per_second=100)
    instances = 0
    discover_calls: list[City | None] = []

    def __init__(self, fetch: FetchText | None = None) -> None:
        type(self).instances += 1

    async def discover(self, city: City | None = None) -> AsyncIterator[str]:
        self.discover_calls.append(city)
        return
        yield

    async def fetch(self, external_id: str) -> EventRecord:
        raise AssertionError("nothing is discovered")


async def test_cycle_builds_one_adapter_and_walks_cities_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(REGISTRY, "counting", CountingAdapter)
    monkeypatch.setattr(CountingAdapter, "instances", 0)
    monkeypatch.setattr(CountingAdapter, "discover_calls", [])
    cities = [resolve("serra"), resolve("guarapari")]
    summaries = await run_cycle("counting", cities=cities, dry_run=True, fetcher=offline_fetcher())
    assert CountingAdapter.instances == 1
    assert CountingAdapter.discover_calls == cities
    assert [s.city for s in summaries] == ["serra", "guarapari"]


async def test_cycle_keeps_a_shared_fetcher_open() -> None:
    fetcher = offline_fetcher()
    await run_cycle("counting", cities=[], dry_run=True, fetcher=fetcher, adapter=CountingAdapter())
    assert not fetcher._client.is_closed  # the sharing contract: only the owner closes it
    await fetcher.aclose()
