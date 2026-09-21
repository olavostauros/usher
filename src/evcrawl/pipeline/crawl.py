"""One crawl run: discover -> fetch -> normalize -> geocode -> upsert (spec task 8).

``run_crawl`` is what ``evcrawl crawl`` calls. It owns one :class:`Fetcher`
and one database session for the run, writes a ``crawl_runs`` row (start,
end, counts, errors) and never lets one bad event stop the others.
``--dry-run`` parses and logs everything and touches nothing.
"""

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evcrawl.config import Settings, get_settings
from evcrawl.db.models import CrawlRun, Event, Source, SourceListing
from evcrawl.db.session import get_sessionmaker
from evcrawl.logging import get_logger
from evcrawl.pipeline.dedupe import flag_duplicates
from evcrawl.pipeline.fetch import Fetcher
from evcrawl.pipeline.geocode import Geocoder, GeocodeStore, MemoryGeocodeStore, SqlGeocodeStore
from evcrawl.pipeline.normalize import NormalizedEvent, normalize
from evcrawl.pipeline.upsert import ACTIVE, mark_missing, upsert_event
from evcrawl.sources import REGISTRY, SourceAdapter

log = get_logger("evcrawl.crawl")

MAX_ERROR_DETAIL = 20  # error lines kept in crawl_runs.error_detail
REFRESH_WINDOW = timedelta(hours=48)  # events starting this soon are re-fetched hourly


@dataclass
class CrawlSummary:
    source: str
    city: str | None
    dry_run: bool
    limit: int | None
    started_at: datetime
    finished_at: datetime | None = None
    crawl_run_id: int | None = None
    discovered: int = 0
    fetched: int = 0
    upserted: int = 0
    created: int = 0
    versions: int = 0
    removed: int = 0
    review: int = 0
    errors: int = 0
    error_lines: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Exit code 1 rule: something was discovered and nothing could be fetched."""
        return self.discovered > 0 and self.fetched == 0

    @property
    def error_detail(self) -> str | None:
        return "\n".join(self.error_lines) or None

    def note_error(self, stage: str, ref: str, exc: BaseException) -> None:
        self.errors += 1
        if len(self.error_lines) < MAX_ERROR_DETAIL:
            self.error_lines.append(f"{stage} {ref}: {type(exc).__name__}: {exc}")


def _log_event(n: NormalizedEvent, dry_run: bool) -> None:
    venue = n.venue
    log.info(
        "crawl.event",
        source=n.source,
        event_external_id=n.external_id,
        title=n.title,
        starts_at=n.starts_at.isoformat(),
        status=n.status.value,
        category=n.category,
        venue=venue.name if venue else None,
        city=venue.city if venue else None,
        latitude=venue.latitude if venue else None,
        longitude=venue.longitude if venue else None,
        price_lots=[(lot.name, str(lot.amount)) for lot in n.price_lots],
        price_lots_pending=n.price_lots_pending,
        dry_run=dry_run,
    )


async def run_crawl(
    source_name: str,
    *,
    city: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    fetcher: Fetcher | None = None,
    adapter: SourceAdapter | None = None,
    urls: Iterable[str] | None = None,
) -> CrawlSummary:
    """Crawl one source. Returns the summary; the ``crawl_runs`` row mirrors it.

    With *urls* discovery is skipped and exactly those listings are re-fetched
    (the hourly refresh); such a run never marks anything as removed.
    """
    settings = settings or get_settings()
    summary = CrawlSummary(source_name, city, dry_run, limit, datetime.now(UTC))
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(
        settings,
        rate_limits={name: cls.rate_limit for name, cls in REGISTRY.items()},
        headers={name: cls.extra_headers for name, cls in REGISTRY.items()},
    )
    adapter = adapter or REGISTRY[source_name](fetcher.fetch_text)

    session: Session | None = None
    store: GeocodeStore = MemoryGeocodeStore()
    run: CrawlRun | None = None
    if not dry_run:
        session = (session_factory or get_sessionmaker())()
        store = SqlGeocodeStore(session)
        run = CrawlRun(source=source_name, started_at=summary.started_at)
        session.add(run)
        session.commit()
        summary.crawl_run_id = run.id
    geocoder = Geocoder(store, fetcher, settings)
    log.info(
        "crawl.start",
        source=source_name,
        city=city,
        dry_run=dry_run,
        limit=limit,
        crawl_run_id=summary.crawl_run_id,
    )

    seen: set[str] = set()
    try:
        async for url in _aiter(urls) if urls is not None else adapter.discover(city):
            if limit is not None and summary.discovered >= limit:
                break
            summary.discovered += 1
            try:
                record = await adapter.fetch(url)
            except Exception as exc:  # one bad page never stops the run
                log.warning("crawl.fetch_failed", source=source_name, url=url, error=str(exc))
                summary.note_error("fetch", url, exc)
                continue
            summary.fetched += 1
            seen.add(record.external_id)
            try:
                n = normalize(record)
                if n.venue is not None:
                    n = n.model_copy(update={"venue": await geocoder.resolve(n.venue)})
                _log_event(n, dry_run)
                if session is None:
                    continue
                result = upsert_event(session, n, crawl_run_id=summary.crawl_run_id)
                summary.upserted += 1
                summary.created += int(result.created)
                summary.versions += int(result.version_written)
                if flag_duplicates(session, n, result.event_id):
                    summary.review += 1
            except Exception as exc:
                log.error(
                    "crawl.event_failed",
                    source=source_name,
                    event_external_id=record.external_id,
                    error=str(exc),
                )
                summary.note_error("upsert", record.external_id, exc)
        full_crawl = urls is None and city is None and limit is None and summary.errors == 0
        if session is not None and full_crawl and summary.fetched > 0:
            summary.removed = mark_missing(session, source_name, seen)
    except Exception as exc:  # discovery itself blew up
        log.error("crawl.discover_failed", source=source_name, city=city, error=str(exc))
        summary.note_error("discover", city or "all", exc)
    finally:
        summary.finished_at = datetime.now(UTC)
        if session is not None and run is not None:
            run.finished_at = summary.finished_at
            run.discovered = summary.discovered
            run.fetched = summary.fetched
            run.upserted = summary.upserted
            run.errors = summary.errors
            run.error_detail = summary.error_detail
            session.commit()
            session.close()
        if own_fetcher:
            await fetcher.aclose()
        log.info(
            "crawl.done",
            source=source_name,
            city=city,
            dry_run=dry_run,
            crawl_run_id=summary.crawl_run_id,
            discovered=summary.discovered,
            fetched=summary.fetched,
            upserted=summary.upserted,
            created=summary.created,
            versions=summary.versions,
            removed=summary.removed,
            review=summary.review,
            errors=summary.errors,
            seconds=round((summary.finished_at - summary.started_at).total_seconds(), 1),
        )
    return summary


async def _aiter(items: Iterable[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


def upcoming_listing_urls(
    session: Session,
    source_name: str,
    *,
    within: timedelta = REFRESH_WINDOW,
    now: datetime | None = None,
) -> list[str]:
    """Source URLs of active events of *source_name* starting within *within* from now."""
    now = now or datetime.now(UTC)
    rows = session.execute(
        select(SourceListing.source_url)
        .join(Event, SourceListing.event_id == Event.id)
        .join(Source, SourceListing.source_id == Source.id)
        .where(
            Source.name == source_name,
            Event.status.in_(ACTIVE),
            Event.starts_at >= now,
            Event.starts_at <= now + within,
        )
        .order_by(Event.starts_at)
    ).all()
    return [url for (url,) in rows]


async def run_refresh(
    source_name: str,
    *,
    within: timedelta = REFRESH_WINDOW,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    fetcher: Fetcher | None = None,
    adapter: SourceAdapter | None = None,
) -> CrawlSummary:
    """Re-fetch every stored listing of *source_name* starting within *within* (AGENTS.md §9)."""
    with (session_factory or get_sessionmaker())() as session:
        urls = upcoming_listing_urls(session, source_name, within=within)
    log.info(
        "refresh.start",
        source=source_name,
        within_hours=within.total_seconds() / 3600,
        urls=len(urls),
    )
    return await run_crawl(
        source_name,
        settings=settings,
        session_factory=session_factory,
        fetcher=fetcher,
        adapter=adapter,
        urls=urls,
    )


__all__ = ["REFRESH_WINDOW", "CrawlSummary", "run_crawl", "run_refresh", "upcoming_listing_urls"]
