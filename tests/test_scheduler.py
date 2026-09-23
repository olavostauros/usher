"""build_scheduler() registers the standing jobs; run_cycle() walks a source's cities.

Nothing is started here and nothing reaches the network: fixtures serve the
Zig feed and a mock transport answers 404 to everything else (missing
``robots.txt`` allows all; a failing geocoder leaves the venue as it is).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from itertools import pairwise

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger

from evcrawl.cities import CITIES, City, for_source, resolve
from evcrawl.config import Settings
from evcrawl.db.models import CrawlKind
from evcrawl.pipeline.crawl import run_cycle, run_refresh
from evcrawl.pipeline.fetch import Fetcher, FetchText
from evcrawl.scheduler import CRAWL_EVERY_HOURS, build_scheduler, cycle_cities
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
    assert len(jobs) == 2 * len(REGISTRY)
    expected = [f"cycle:{s}" for s in REGISTRY] + [f"refresh:{s}" for s in REGISTRY]
    assert sorted(jobs) == sorted(expected)
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
    for job in sched.get_jobs():
        assert job.kwargs["fetcher"] is fetcher


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
