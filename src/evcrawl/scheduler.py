"""Cron-style scheduling of crawl jobs with APScheduler (AGENTS.md §2, §9; phase 3 national §3).

Standing jobs per source: one **cycle** every 6 hours that walks every enabled
registry city of the source in order through one adapter (``run_cycle``), and
one hourly **refresh** of every stored event starting within 48 hours
(``run_refresh``). Sources run concurrently, each in its own rate-limit
bucket; a cycle slower than 6 h skips the next trigger instead of overlapping
itself (``coalesce=True, max_instances=1``). One :class:`Fetcher` is built per
scheduler process and handed to every job, so a source's cycle and its refresh
share the source's bucket, and every run shares the geocoder's.

One more job, **heartbeat**, upserts the single ``scheduler_heartbeat`` row
every minute (``beat``), so "is the scheduler alive?" is a SQL question
(``v_scheduler``) answerable from inside the workspace, where containers are
invisible (``docs/specs/phase-3-monitoring.md`` §3).

Runs only in the ``runtime`` image (the image's default command); never from
the workspace.
"""

import asyncio
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from evcrawl.cities import for_source
from evcrawl.config import Settings
from evcrawl.db.models import SchedulerHeartbeat
from evcrawl.db.session import get_sessionmaker
from evcrawl.logging import get_logger
from evcrawl.pipeline.crawl import build_fetcher, run_cycle, run_refresh
from evcrawl.pipeline.fetch import Fetcher
from evcrawl.sources import REGISTRY

CRAWL_EVERY_HOURS = 6
REFRESH_MINUTE = 30  # hourly refresh, off the hour the cycles start on
HEARTBEAT_SECONDS = 60
HEARTBEAT_JOB_ID = "heartbeat"


def cycle_cities() -> dict[str, list[str]]:
    """Source -> the registry slugs its cycle walks (``evcrawl.cities``, enabled only)."""
    return {name: [city.slug for city in for_source(name)] for name in REGISTRY}


def beat(
    sched: AsyncIOScheduler,
    settings: Settings,
    started_at: datetime,
    session_factory: sessionmaker[Session] | None = None,
) -> None:
    """Upsert the ``scheduler_heartbeat`` row: this process, now, its build, its job count.

    ``jobs`` counts every registered job, this one included. A new process
    overwrites the previous one's ``started_at``, so ``v_scheduler`` always
    describes the scheduler that is beating. Runs in APScheduler's thread pool
    (sync: the engine is sync); a database error is one ``heartbeat.failed``
    log line and the next beat retries, so a blip never stops the crawl jobs
    and status.py sees it as a stale heartbeat, which is what it is.
    """
    log = get_logger("evcrawl.scheduler")
    now = datetime.now(UTC)
    values = {
        "id": 1,
        "started_at": started_at,
        "beat_at": now,
        "git_sha": settings.evcrawl_git_sha,
        "jobs": len(sched.get_jobs()),
    }
    stmt = insert(SchedulerHeartbeat).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=values)
    try:
        with (session_factory or get_sessionmaker())() as session:
            session.execute(stmt)
            session.commit()
    except SQLAlchemyError as e:
        log.warning("heartbeat.failed", error=str(e).splitlines()[0][:300])


def build_scheduler(
    settings: Settings,
    fetcher: Fetcher,
    session_factory: sessionmaker[Session] | None = None,
) -> AsyncIOScheduler:
    """Return a scheduler with the standing jobs registered (not started).

    Every crawl job gets *fetcher*: the one process-wide :class:`Fetcher`. The
    heartbeat fires once as soon as the scheduler starts, then every
    :data:`HEARTBEAT_SECONDS`; *session_factory* only exists for the tests.
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
    sched.add_job(
        beat,
        IntervalTrigger(seconds=HEARTBEAT_SECONDS),
        kwargs={
            "sched": sched,
            "settings": settings,
            "started_at": datetime.now(UTC),
            "session_factory": session_factory,
        },
        id=HEARTBEAT_JOB_ID,
        name=f"heartbeat: upsert scheduler_heartbeat every {HEARTBEAT_SECONDS}s",
        next_run_time=datetime.now(UTC),  # the first beat is at start, not a minute later
        coalesce=True,
        max_instances=1,
        misfire_grace_time=HEARTBEAT_SECONDS,
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
    log.info(
        "scheduler.started",
        jobs=len(jobs),
        ids=[j.id for j in jobs],
        cycles=cycle_cities(),
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    try:
        await asyncio.Event().wait()
    finally:
        sched.shutdown(wait=False)
        await fetcher.aclose()
        log.info("scheduler.stopped")
