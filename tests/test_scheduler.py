"""build_scheduler() registers the standing jobs; nothing is started here."""

from datetime import UTC, datetime
from itertools import pairwise

from apscheduler.triggers.cron import CronTrigger

from evcrawl.config import Settings
from evcrawl.pipeline.crawl import run_crawl, run_refresh
from evcrawl.scheduler import (
    CITY_STAGGER_MINUTES,
    CRAWL_EVERY_HOURS,
    SOURCE_CITIES,
    build_scheduler,
)
from evcrawl.sources.sympla import CITY_SLUGS


def test_build_scheduler_registers_six_city_crawls_and_one_refresh() -> None:
    sched = build_scheduler(Settings())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert len(jobs) == 7
    assert sorted(jobs) == sorted(
        [f"crawl:sympla:{city}" for city in CITY_SLUGS] + ["refresh:sympla"]
    )
    assert SOURCE_CITIES["sympla"] == list(CITY_SLUGS)
    for city in CITY_SLUGS:
        job = jobs[f"crawl:sympla:{city}"]
        assert job.func is run_crawl
        assert job.args == ("sympla",)
        assert job.kwargs["city"] == city
        assert job.max_instances == 1 and job.coalesce
    refresh = jobs["refresh:sympla"]
    assert refresh.func is run_refresh and refresh.args == ("sympla",)
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


def test_city_crawls_run_every_six_hours_staggered() -> None:
    sched = build_scheduler(Settings())
    start = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    firsts = []
    for city in CITY_SLUGS:
        trigger = sched.get_job(f"crawl:sympla:{city}").trigger
        assert isinstance(trigger, CronTrigger)
        times = fire_times(trigger, start, 3)
        assert (times[1] - times[0]).total_seconds() == CRAWL_EVERY_HOURS * 3600
        assert (times[2] - times[1]).total_seconds() == CRAWL_EVERY_HOURS * 3600
        firsts.append(times[0])
    gaps = [(b - a).total_seconds() for a, b in pairwise(firsts)]
    assert gaps == [CITY_STAGGER_MINUTES * 60] * (len(CITY_SLUGS) - 1)  # 10 min apart, in order


def test_refresh_runs_hourly() -> None:
    sched = build_scheduler(Settings())
    trigger = sched.get_job("refresh:sympla").trigger
    assert isinstance(trigger, CronTrigger)
    times = fire_times(trigger, datetime(2026, 9, 21, 0, 0, tzinfo=UTC), 3)
    assert [(b - a).total_seconds() for a, b in pairwise(times)] == [3600, 3600]
    assert all(t.minute != 0 for t in times)  # never on the hour, when city crawls start
