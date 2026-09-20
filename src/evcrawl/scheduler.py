"""Cron-style scheduling of crawl jobs with APScheduler (AGENTS.md §2, §8)."""

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from evcrawl.config import Settings
from evcrawl.logging import get_logger


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Return a scheduler with the standing crawl jobs registered.

    No jobs yet: adapters are registered here once they exist (AGENTS.md §5 step 7).
    """
    return AsyncIOScheduler(timezone="UTC")


async def run_forever(settings: Settings) -> None:
    """Start the scheduler and block until cancelled."""
    log = get_logger("evcrawl.scheduler")
    sched = build_scheduler(settings)
    sched.start()
    log.info("scheduler.started", jobs=len(sched.get_jobs()))
    try:
        await asyncio.Event().wait()
    finally:
        sched.shutdown(wait=False)
        log.info("scheduler.stopped")
