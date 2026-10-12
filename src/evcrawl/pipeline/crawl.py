"""One crawl run: discover -> fetch -> normalize -> geocode -> dedupe -> upsert (spec task 8).

``run_crawl`` is what ``evcrawl crawl`` calls. It owns one database session
for the run, writes a ``crawl_runs`` row (start, end, counts, errors) and never
lets one bad event stop the others. It builds its own :class:`Fetcher` unless
one is passed in: the CLI gets one per invocation, the scheduler shares one
process-wide fetcher between every job (phase 3 national, §3). ``run_cycle``
is the scheduler's unit: every registry city of one source, in order, through
one adapter. ``--dry-run`` parses and logs everything and touches nothing.

Conditional fetch (ADR 0013): a listing an adapter yields as ``Discovered``
carries a fingerprint of the listing item; when it equals the one stored on
the ``source_listings`` row and the event does not start within
``REFRESH_WINDOW``, the event page is skipped, the listing counts as seen and
``skipped`` goes up. A dry run has no session to compare against and always
fetches.

A gone page (ADR 0022): when the source answers 404 or 410 for a listing's
own URL, the source has dropped the event. The run counts it as ``gone``,
not as an error, and stamps the stored listing removed (``mark_gone``), but
only when the run fetched or skipped more listings than it found gone;
otherwise every gone page is an error, so a source that moved its URLs (every
fingerprint changes, nothing is skipped, every page 404s) never empties the
catalogue. Only adapters whose ``fetch`` requests the listing URL itself
(Sympla, Zig, TicketBolt) can report a gone page this way.
"""

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evcrawl.cities import City, for_source
from evcrawl.config import Settings, get_settings
from evcrawl.db.models import CrawlKind, CrawlRun, Event, Source, SourceListing
from evcrawl.db.session import get_sessionmaker
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import Fetcher, FetchError
from evcrawl.pipeline.geocode import Geocoder, GeocodeStore, MemoryGeocodeStore, SqlGeocodeStore
from evcrawl.pipeline.normalize import NormalizedEvent, normalize
from evcrawl.pipeline.upsert import ACTIVE, mark_gone, mark_missing, skip_unchanged, upsert_event
from evcrawl.sources import REGISTRY, Discovered, SourceAdapter

log = get_logger("evcrawl.crawl")

MAX_ERROR_DETAIL = 20  # error lines kept in crawl_runs.error_detail
REFRESH_WINDOW = timedelta(hours=48)  # events starting this soon are re-fetched hourly
GONE_STATUSES = frozenset({404, 410})  # the source's answer that a listing's page is gone


@dataclass
class CrawlSummary:
    source: str
    city: str | None  # registry slug
    kind: CrawlKind
    dry_run: bool
    limit: int | None
    started_at: datetime
    finished_at: datetime | None = None
    crawl_run_id: int | None = None
    discovered: int = 0
    fetched: int = 0
    skipped: int = 0  # listings whose fingerprint was unchanged (ADR 0013)
    upserted: int = 0
    created: int = 0
    merged: int = 0  # new listings linked to an existing event (ADR 0010)
    versions: int = 0
    removed: int = 0  # listings marked removed_at (ADR 0012, ADR 0022)
    gone: int = 0  # pages the source answered 404/410 for, taken as removals (ADR 0022)
    review: int = 0
    errors: int = 0
    error_lines: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Exit code 1 rule: something discovered, nothing fetched, and nothing skipped or an error.

        Unchanged listings (ADR 0013) alone are a healthy run; next to a
        failure they no longer hide a source whose every page fails (the
        TicketBolt waiting room of 2026-09-24 passed the release smoke with
        9 skipped and 1 failed).
        """
        return self.discovered > 0 and self.fetched == 0 and (self.skipped == 0 or self.errors > 0)

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
        timezone=n.timezone,
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


def build_fetcher(settings: Settings | None = None) -> Fetcher:
    """A :class:`Fetcher` seeded with every registered source's rate limit and headers.

    The CLI builds one per invocation (inside ``run_crawl``); the scheduler
    builds one per process and hands it to every job, so a source's cycle and
    its hourly refresh share the source's bucket, and every run shares the
    geocoder's.
    """
    return Fetcher(
        settings or get_settings(),
        rate_limits={name: cls.rate_limit for name, cls in REGISTRY.items()},
        headers={name: cls.extra_headers for name, cls in REGISTRY.items()},
    )


async def run_crawl(
    source_name: str,
    *,
    city: City | None = None,
    kind: CrawlKind = CrawlKind.CRAWL,
    dry_run: bool = False,
    limit: int | None = None,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    fetcher: Fetcher | None = None,
    adapter: SourceAdapter | None = None,
    urls: Iterable[str] | None = None,
) -> CrawlSummary:
    """Crawl one source, for one registry *city* or every enabled one (``None``).

    Returns the summary; the ``crawl_runs`` row mirrors it, tagged with the
    city and with *kind* — freshness is measured on ``crawl`` rows, so the
    hourly refresh and hand-run crawls pass ``refresh`` and ``manual``. With
    *urls* discovery is skipped and exactly those listings are re-fetched (the
    hourly refresh); such a run never marks anything as removed and, yielding
    plain URLs, never skips one.
    """
    settings = settings or get_settings()
    slug = city.slug if city is not None else None
    summary = CrawlSummary(source_name, slug, kind, dry_run, limit, datetime.now(UTC))
    own_fetcher = fetcher is None
    fetcher = fetcher or build_fetcher(settings)
    adapter = adapter or REGISTRY[source_name](fetcher.fetch_text)

    session: Session | None = None
    store: GeocodeStore = MemoryGeocodeStore()
    run: CrawlRun | None = None
    if not dry_run:
        session = (session_factory or get_sessionmaker())()
        store = SqlGeocodeStore(session)
        run = CrawlRun(source=source_name, city=slug, kind=kind, started_at=summary.started_at)
        session.add(run)
        session.commit()
        summary.crawl_run_id = run.id
    geocoder = Geocoder(store, fetcher, settings)
    log.info(
        "crawl.start",
        source=source_name,
        city=slug,
        kind=kind.value,
        dry_run=dry_run,
        limit=limit,
        crawl_run_id=summary.crawl_run_id,
    )

    seen: set[str] = set()
    gone: list[tuple[str, FetchError]] = []
    gone_ids: set[str] = set()  # a Sympla listing may store ``newUrl``: match by id too
    try:
        async for item in _aiter(urls) if urls is not None else adapter.discover(city):
            if limit is not None and summary.discovered >= limit:
                break
            summary.discovered += 1
            url = item.url if isinstance(item, Discovered) else item
            fingerprint = item.fingerprint if isinstance(item, Discovered) else None
            if session is not None and isinstance(item, Discovered):
                try:
                    unchanged = skip_unchanged(
                        session,
                        source_name,
                        item.external_id,
                        item.fingerprint,
                        city=slug,
                        crawl_run_id=summary.crawl_run_id,
                        within=REFRESH_WINDOW,
                    )
                except Exception as exc:  # fail open: fetch the page as before
                    session.rollback()
                    log.warning("crawl.skip_failed", source=source_name, url=url, error=str(exc))
                    summary.note_error("skip", url, exc)
                    unchanged = False
                if unchanged:
                    summary.skipped += 1
                    seen.add(item.external_id)
                    continue
            try:
                record = await adapter.fetch(url)
            except Exception as exc:  # one bad page never stops the run
                if isinstance(exc, FetchError) and exc.url == url and exc.status in GONE_STATUSES:
                    log.info("crawl.page_gone", source=source_name, url=url, status=exc.status)
                    gone.append((url, exc))
                    if isinstance(item, Discovered):
                        gone_ids.add(item.external_id)
                    continue
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
                result = upsert_event(
                    session,
                    n,
                    crawl_run_id=summary.crawl_run_id,
                    city=slug,
                    fingerprint=fingerprint,
                )
                summary.upserted += 1
                summary.created += int(result.created)
                summary.merged += int(result.merged)
                summary.versions += int(result.version_written)
                summary.review += int(result.review)
            except Exception as exc:
                log.error(
                    "crawl.event_failed",
                    source=source_name,
                    event_external_id=record.external_id,
                    error=str(exc),
                )
                summary.note_error("upsert", record.external_id, exc)
        if gone and summary.fetched + summary.skipped > len(gone):
            summary.gone = len(gone)
            if session is not None:
                summary.removed += mark_gone(
                    session, source_name, [u for u, _ in gone], external_ids=gone_ids
                )
        else:  # nothing, or too little, worked: the 404s may be ours, not the source's
            for u, err in gone:
                summary.note_error("fetch", u, err)
        # A complete look at the city (or at every city): what it did not list is gone.
        complete = urls is None and limit is None and summary.errors == 0
        if session is not None and complete and summary.fetched + summary.skipped > 0:
            summary.removed += mark_missing(session, source_name, slug, seen)
    except Exception as exc:  # discovery itself blew up
        log.error("crawl.discover_failed", source=source_name, city=slug, error=str(exc))
        summary.note_error("discover", slug or "all", exc)
    finally:
        summary.finished_at = datetime.now(UTC)
        if session is not None and run is not None:
            run.finished_at = summary.finished_at
            run.discovered = summary.discovered
            run.fetched = summary.fetched
            run.skipped = summary.skipped
            run.upserted = summary.upserted
            run.created = summary.created
            run.merged = summary.merged
            run.versions = summary.versions
            run.removed = summary.removed
            run.review = summary.review
            run.errors = summary.errors
            run.error_detail = summary.error_detail
            session.commit()
            session.close()
        if own_fetcher:
            await fetcher.aclose()
        log.info(
            "crawl.done",
            source=source_name,
            city=slug,
            kind=kind.value,
            dry_run=dry_run,
            crawl_run_id=summary.crawl_run_id,
            discovered=summary.discovered,
            fetched=summary.fetched,
            skipped=summary.skipped,
            upserted=summary.upserted,
            created=summary.created,
            merged=summary.merged,
            versions=summary.versions,
            removed=summary.removed,
            gone=summary.gone,
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
            SourceListing.removed_at.is_(None),  # gone already: the cycle revives it if listed
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
    """Re-fetch every stored listing of *source_name* starting within *within* (AGENTS.md §9).

    Listings the adapter cannot rebuild from their URL alone (``refetchable``)
    are left to the next cycle, which sees them in the listing walk.
    """
    with (session_factory or get_sessionmaker())() as session:
        stored = upcoming_listing_urls(session, source_name, within=within)
    cls = type(adapter) if adapter is not None else REGISTRY[source_name]
    urls = [url for url in stored if cls.refetchable(url)]
    log.info(
        "refresh.start",
        source=source_name,
        within_hours=within.total_seconds() / 3600,
        urls=len(urls),
        listing_only=len(stored) - len(urls),  # left to the next cycle's listing walk
    )
    return await run_crawl(
        source_name,
        kind=CrawlKind.REFRESH,
        settings=settings,
        session_factory=session_factory,
        fetcher=fetcher,
        adapter=adapter,
        urls=urls,
    )


async def run_cycle(
    source_name: str,
    *,
    cities: Sequence[City] | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    fetcher: Fetcher | None = None,
    adapter: SourceAdapter | None = None,
) -> list[CrawlSummary]:
    """One scheduler cycle of *source_name*: every registry city, in order, one adapter.

    *cities* defaults to the enabled registry cities the source lists
    (``for_source``). The adapter is built once for the whole cycle, so a
    state- or national-level source (Zig's feed, BlueTicket's per-UF listing,
    TicketBolt's national query) is listed once per cycle, not once per city.
    Each city is its own ``crawl`` run and ``crawl_runs`` row — the unit
    freshness is measured on — and a failing city never stops the next one
    (``run_crawl`` never raises). Returns one summary per city.
    """
    settings = settings or get_settings()
    cities = list(cities if cities is not None else for_source(source_name))
    slugs = [city.slug for city in cities]
    own_fetcher = fetcher is None
    fetcher = fetcher or build_fetcher(settings)
    adapter = adapter or REGISTRY[source_name](fetcher.fetch_text)
    started_at = datetime.now(UTC)
    log.info("cycle.start", source=source_name, cities=slugs, dry_run=dry_run)
    summaries: list[CrawlSummary] = []
    try:
        for city in cities:
            summaries.append(
                await run_crawl(
                    source_name,
                    city=city,
                    kind=CrawlKind.CRAWL,
                    dry_run=dry_run,
                    settings=settings,
                    session_factory=session_factory,
                    fetcher=fetcher,
                    adapter=adapter,
                )
            )
    finally:
        if own_fetcher:
            await fetcher.aclose()
        log.info(
            "cycle.done",
            source=source_name,
            cities=slugs,
            dry_run=dry_run,
            runs=len(summaries),
            discovered=sum(s.discovered for s in summaries),
            fetched=sum(s.fetched for s in summaries),
            skipped=sum(s.skipped for s in summaries),
            upserted=sum(s.upserted for s in summaries),
            removed=sum(s.removed for s in summaries),
            errors=sum(s.errors for s in summaries),
            seconds=round((datetime.now(UTC) - started_at).total_seconds(), 1),
        )
    return summaries


__all__ = [
    "REFRESH_WINDOW",
    "CrawlSummary",
    "build_fetcher",
    "run_crawl",
    "run_cycle",
    "run_refresh",
    "upcoming_listing_urls",
]
