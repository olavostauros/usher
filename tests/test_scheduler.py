"""build_scheduler() registers the standing jobs; nothing is started here."""

from datetime import UTC, datetime
from itertools import pairwise

import pytest
from apscheduler.triggers.cron import CronTrigger

from evcrawl.cities import GREATER_VITORIA, for_source
from evcrawl.config import Settings
from evcrawl.pipeline.crawl import run_crawl, run_refresh
from evcrawl.scheduler import (
    CITY_STAGGER_MINUTES,
    CRAWL_EVERY_HOURS,
    SOURCE_CITIES,
    build_scheduler,
)
from evcrawl.sources import REGISTRY

SOURCES = sorted(SOURCE_CITIES)


def test_every_registered_source_has_city_jobs() -> None:
    assert set(SOURCE_CITIES) == set(REGISTRY)
    for source, cities in SOURCE_CITIES.items():
        assert cities == for_source(source) == list(GREATER_VITORIA)  # the capitals wait for task 8
        assert all(city.enabled and city.key(source) for city in cities)


def test_build_scheduler_registers_six_city_crawls_and_one_refresh_per_source() -> None:
    sched = build_scheduler(Settings())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert len(jobs) == 7 * len(SOURCE_CITIES)
    expected = [f"crawl:{s}:{c.slug}" for s, cities in SOURCE_CITIES.items() for c in cities]
    expected += [f"refresh:{s}" for s in SOURCE_CITIES]
    assert sorted(jobs) == sorted(expected)
    for source, cities in SOURCE_CITIES.items():
        for city in cities:
            job = jobs[f"crawl:{source}:{city.slug}"]
            assert job.func is run_crawl
            assert job.args == (source,)
            assert job.kwargs["city"] is city
            assert job.max_instances == 1 and job.coalesce
        refresh = jobs[f"refresh:{source}"]
        assert refresh.func is run_refresh and refresh.args == (source,)
        assert "city" not in refresh.kwargs


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
def test_city_crawls_run_every_six_hours_staggered(source: str) -> None:
    sched = build_scheduler(Settings())
    start = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    firsts = []
    cities = SOURCE_CITIES[source]
    for city in cities:
        trigger = sched.get_job(f"crawl:{source}:{city.slug}").trigger
        assert isinstance(trigger, CronTrigger)
        times = fire_times(trigger, start, 3)
        assert (times[1] - times[0]).total_seconds() == CRAWL_EVERY_HOURS * 3600
        assert (times[2] - times[1]).total_seconds() == CRAWL_EVERY_HOURS * 3600
        firsts.append(times[0])
    gaps = [(b - a).total_seconds() for a, b in pairwise(firsts)]
    assert gaps == [CITY_STAGGER_MINUTES * 60] * (len(cities) - 1)  # 10 min apart, in order


@pytest.mark.parametrize("source", SOURCES)
def test_refresh_runs_hourly(source: str) -> None:
    sched = build_scheduler(Settings())
    trigger = sched.get_job(f"refresh:{source}").trigger
    assert isinstance(trigger, CronTrigger)
    times = fire_times(trigger, datetime(2026, 9, 21, 0, 0, tzinfo=UTC), 3)
    assert [(b - a).total_seconds() for a, b in pairwise(times)] == [3600, 3600]
    assert all(t.minute != 0 for t in times)  # never on the hour, when city crawls start
