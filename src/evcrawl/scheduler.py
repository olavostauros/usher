"""Cron-style scheduling of crawl jobs with APScheduler (AGENTS.md §2, §9; phase 3 national §3).

Standing jobs per source: one **cycle** every 6 hours that walks every enabled
registry city of the source in order through one adapter (``run_cycle``), and
one hourly **refresh** of every stored event starting within 48 hours
(``run_refresh``). Sources run concurrently, each in its own rate-limit
bucket; a cycle slower than 6 h skips the next trigger instead of overlapping
itself (``coalesce=True, max_instances=1``). One :class:`Fetcher` is built per
scheduler process and handed to every job, so a source's cycle and its refresh
share the source's bucket, and every run shares the geocoder's.

Runs only in the ``runtime`` image (the image's default command); never from
the workspace.
"""

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from evcrawl.cities import for_source
from evcrawl.config import Settings
from evcrawl.logging import get_logger
from evcrawl.pipeline.crawl import build_fetcher, run_cycle, run_refresh
from evcrawl.pipeline.fetch import Fetcher
from evcrawl.sources import REGISTRY

CRAWL_EVERY_HOURS = 6
REFRESH_MINUTE = 30  # hourly refresh, off the hour the cycles start on


def cycle_cities() -> dict[str, list[str]]:
    """Source -> the registry slugs its cycle walks (``evcrawl.cities``, enabled only)."""
    return {name: [city.slug for city in for_source(name)] for name in REGISTRY}


def build_scheduler(settings: Settings, fetcher: Fetcher) -> AsyncIOScheduler:
    """Return a scheduler with the standing jobs registered (not started).

    Every job gets *fetcher*: the one process-wide :class:`Fetcher`.
    """
    sched = AsyncIOScheduler(timezone="UTC")
    for source, slugs in cycle_cities().items():
        sched.add_job(
            run_cycle,
            CronTrigger(hour=f"*/{CRAWL_EVERY_HOURS}", minute=0),
            args=[source],
            kwargs={"settings": settings, "fetcher": fetcher},
            id=f"cycle:{source}",
            name=f"cycle {source} over {len(slugs)} cities every {CRAWL_EVERY_HOURS}h",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        sched.add_job(
            run_refresh,
            CronTrigger(minute=REFRESH_MINUTE),
            args=[source],
            kwargs={"settings": settings, "fetcher": fetcher},
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
    # Built inside the running loop: the HTTP client and the bucket locks bind to it.
    fetcher = build_fetcher(settings)
    sched = build_scheduler(settings, fetcher)
    sched.start()
    jobs = sched.get_jobs()
    log.info("scheduler.started", jobs=len(jobs), ids=[j.id for j in jobs], cycles=cycle_cities())
    try:
        await asyncio.Event().wait()
    finally:
        sched.shutdown(wait=False)
        await fetcher.aclose()
        log.info("scheduler.stopped")
