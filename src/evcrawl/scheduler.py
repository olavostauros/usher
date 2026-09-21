"""Cron-style scheduling of crawl jobs with APScheduler (AGENTS.md §2, §9, spec task 9).

Standing jobs per source: one full crawl per city every 6 hours, staggered so
the shared rate limit is never contended, plus one hourly refresh of every
stored event starting within 48 hours. Runs only in the ``runtime`` image
(the image's default command); never from the workspace.
"""

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from evcrawl.config import Settings
from evcrawl.logging import get_logger
from evcrawl.pipeline.crawl import run_crawl, run_refresh
from evcrawl.sources.blueticket import CITY_NAMES as BLUETICKET_CITIES
from evcrawl.sources.sympla import CITY_SLUGS

CRAWL_EVERY_HOURS = 6
CITY_STAGGER_MINUTES = 10  # city N starts N * 10 min after the hour
REFRESH_MINUTE = 30  # hourly refresh, between the city crawls

# source -> cities it crawls; Phase 2 sources register here. Sources run
# concurrently (each has its own rate-limit bucket); cities of one source are
# staggered so its bucket is never contended.
SOURCE_CITIES: dict[str, list[str]] = {
    "sympla": list(CITY_SLUGS),
    "blueticket": list(BLUETICKET_CITIES),
}


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Return a scheduler with the standing jobs registered (not started)."""
    sched = AsyncIOScheduler(timezone="UTC")
    for source, cities in SOURCE_CITIES.items():
        for i, city in enumerate(cities):
            sched.add_job(
                run_crawl,
                CronTrigger(hour=f"*/{CRAWL_EVERY_HOURS}", minute=i * CITY_STAGGER_MINUTES),
                args=[source],
                kwargs={"city": city, "settings": settings},
                id=f"crawl:{source}:{city}",
                name=f"crawl {source} {city} every {CRAWL_EVERY_HOURS}h",
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
        sched.add_job(
            run_refresh,
            CronTrigger(minute=REFRESH_MINUTE),
            args=[source],
            kwargs={"settings": settings},
            id=f"refresh:{source}",
            name=f"refresh {source} events starting within 48h, hourly",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )
    return sched


async def run_forever(settings: Settings) -> None:
    """Start the scheduler and block until cancelled."""
    log = get_logger("evcrawl.scheduler")
    sched = build_scheduler(settings)
    sched.start()
    jobs = sched.get_jobs()
    log.info("scheduler.started", jobs=len(jobs), ids=[j.id for j in jobs])
    try:
        await asyncio.Event().wait()
    finally:
        sched.shutdown(wait=False)
        log.info("scheduler.stopped")
