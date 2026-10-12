"""Writing events to the database with versioning (AGENTS.md §7, spec task 6).

``upsert_event`` runs one transaction per event: get-or-create the venue,
organizer and category, upsert the ``source_listings`` row, insert or update
the canonical ``events`` row with its status transition, append an
``event_versions`` row only when the normalized content hash (or the status)
changed, and store a gzip of the source payload in ``raw_snapshots`` once per
distinct payload. Nothing is ever deleted; ``mark_missing`` stamps
``removed_at`` on the listings a complete crawl of one city did not see, and
an event becomes ``removed_from_source`` only once all its listings are
removed (ADR 0012).

Cross-source dedupe (Phase 2 task 9, ADR 0010): a listing seen for the first
time is matched against ``events`` (``pipeline/dedupe.py``). One confident
candidate takes the listing as a secondary; the event's oldest listing stays
the *primary* and its content wins, secondaries only fill fields the row has
empty. An ambiguous match creates a separate event with ``dedupe_review``.
"""

import gzip
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from geoalchemy2 import WKTElement
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from evcrawl.db.models import (
    Category,
    Event,
    EventStatus,
    EventVersion,
    Organizer,
    PriceLot,
    RawSnapshot,
    Source,
    SourceListing,
    Venue,
)
from evcrawl.logging import get_logger
from evcrawl.pipeline.dedupe import flag_duplicates, flag_for_review, match
from evcrawl.pipeline.normalize import (
    CATEGORIES,
    UNCATEGORIZED,
    NormalizedEvent,
    NormalizedOrganizer,
    NormalizedVenue,
)
from evcrawl.sources import REGISTRY

log = get_logger("evcrawl.pipeline.upsert")

ACTIVE = frozenset({EventStatus.SCHEDULED, EventStatus.UPDATED})


@dataclass(frozen=True)
class UpsertResult:
    event_id: int
    listing_id: int
    created: bool  # a new ``events`` row
    version_written: bool
    snapshot_written: bool
    status: EventStatus
    merged: bool = False  # a new listing linked to an existing event (ADR 0010)
    review: bool = False  # the event was flagged ``dedupe_review`` in this upsert


# --- pure helpers


def next_status(current: EventStatus | None, incoming: EventStatus, changed: bool) -> EventStatus:
    """Status transition for an event row (spec §3).

    ``incoming`` is what normalization derived from the source (scheduled,
    cancelled or ended); ``changed`` says whether the content hash moved.
    """
    if incoming in (EventStatus.CANCELLED, EventStatus.ENDED):
        return incoming
    if current is None:
        return EventStatus.SCHEDULED
    if changed:
        return EventStatus.UPDATED
    if current == EventStatus.REMOVED_FROM_SOURCE:
        return EventStatus.SCHEDULED
    return current


def payload_bytes(raw: dict[str, object]) -> bytes:
    """Canonical JSON bytes of the source payload (what gets hashed and gzipped)."""
    return json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def snapshot_of(raw: dict[str, object]) -> tuple[str, bytes]:
    """``(sha256 of the JSON, gzip of the JSON)``."""
    data = payload_bytes(raw)
    return hashlib.sha256(data).hexdigest(), gzip.compress(data, mtime=0)


# --- get-or-create


def get_or_create_source(session: Session, name: str) -> Source:
    source = session.scalar(select(Source).where(Source.name == name))
    if source is None:
        adapter = REGISTRY.get(name)
        source = Source(name=name, base_url=adapter.base_url if adapter else "")
        session.add(source)
        session.flush()
    return source


def get_or_create_category(session: Session, slug: str) -> Category:
    category = session.scalar(select(Category).where(Category.slug == slug))
    if category is None:
        category = Category(slug=slug, name=CATEGORIES.get(slug, slug))
        session.add(category)
        session.flush()
    return category


def get_or_create_organizer(session: Session, organizer: NormalizedOrganizer) -> Organizer:
    row = session.scalar(
        select(Organizer).where(Organizer.name_normalized == organizer.name_normalized)
    )
    if row is None:
        row = Organizer(
            name=organizer.name, name_normalized=organizer.name_normalized, url=organizer.url
        )
        session.add(row)
    else:
        row.name = organizer.name
        if organizer.url:
            row.url = organizer.url
    session.flush()
    return row


def get_or_create_venue(session: Session, venue: NormalizedVenue) -> Venue:
    row = session.scalar(select(Venue).where(Venue.address_normalized == venue.address_normalized))
    if row is None:
        row = Venue(address_normalized=venue.address_normalized)
        session.add(row)
    # Fill or refresh with whatever the source knows; never blank a field.
    row.name = venue.name or row.name
    row.address = venue.address or row.address
    row.city = venue.city or row.city
    row.state = venue.state or row.state
    row.postal_code = venue.postal_code or row.postal_code
    if venue.latitude is not None and venue.longitude is not None:
        row.location = WKTElement(f"POINT({venue.longitude} {venue.latitude})", srid=4326)
    session.flush()
    return row


# --- the upsert


def _lots(n: NormalizedEvent) -> list[PriceLot]:
    return [
        PriceLot(name=lot.name, amount=lot.amount, currency=lot.currency, position=i)
        for i, lot in enumerate(n.price_lots)
    ]


def _apply_content(event: Event, n: NormalizedEvent, *, keep_filled: bool) -> None:
    """The primary listing's content becomes the event's.

    With *keep_filled* (the event has secondary listings) an empty value from
    the primary does not blank what a secondary filled in (ADR 0010); a
    single-listing event mirrors its source exactly, as in Phase 1.
    """
    event.title = n.title
    event.title_normalized = n.title_normalized
    event.starts_at = n.starts_at
    event.timezone = n.timezone
    event.is_online = n.is_online
    event.content_hash = n.content_hash
    if keep_filled:
        event.description = n.description or event.description
        event.ends_at = n.ends_at or event.ends_at
        event.image_url = n.image_url or event.image_url
        if n.price_lots:
            event.price_lots = _lots(n)
    else:
        event.description = n.description
        event.ends_at = n.ends_at
        event.image_url = n.image_url
        event.price_lots = _lots(n)


def _fill_empty(
    event: Event,
    n: NormalizedEvent,
    venue: Venue | None,
    organizer: Organizer | None,
    category: Category,
) -> list[str]:
    """A secondary listing fills what the event still lacks; returns the field names filled."""
    filled: list[str] = []
    if not event.description and n.description:
        event.description = n.description
        filled.append("description")
    if event.ends_at is None and n.ends_at is not None:
        event.ends_at = n.ends_at
        filled.append("ends_at")
    if not event.image_url and n.image_url:
        event.image_url = n.image_url
        filled.append("image_url")
    if event.venue is None and venue is not None:
        event.venue = venue
        filled.append("venue")
    if event.organizer is None and organizer is not None:
        event.organizer = organizer
        filled.append("organizer")
    if (event.category is None or event.category.slug == UNCATEGORIZED) and (
        category.slug != UNCATEGORIZED
    ):
        event.category = category
        filled.append("category")
    if not event.price_lots and n.price_lots:
        event.price_lots = _lots(n)
        filled.append("price_lots")
    return filled


def _is_primary(event: Event, listing: SourceListing) -> bool:
    """The oldest listing of an event is its primary (first seen wins, ADR 0010)."""
    oldest = min(event.listings, key=lambda row: (row.first_seen_at, row.id))
    return oldest is listing


def _version_content(n: NormalizedEvent, status: EventStatus) -> dict[str, object]:
    return {
        "title": n.title,
        "description": n.description,
        "starts_at": n.starts_at.isoformat(),
        "ends_at": n.ends_at.isoformat() if n.ends_at else None,
        "timezone": n.timezone,
        "status": status.value,
        "is_online": n.is_online,
        "image_url": n.image_url,
        "venue": n.venue.model_dump() if n.venue else None,
        "organizer": n.organizer.model_dump() if n.organizer else None,
        "category": n.category,
        "price_lots": [
            {"name": lot.name, "amount": str(lot.amount), "currency": lot.currency}
            for lot in n.price_lots
        ],
        "price_lots_pending": n.price_lots_pending,
        "source_url": n.source_url,
    }


def _merge_content(
    n: NormalizedEvent, status: EventStatus, reason: str, filled: list[str]
) -> dict[str, object]:
    """Version row of a secondary listing: what it was and which fields it filled."""
    content = _version_content(n, status)
    return {
        "status": status.value,
        "reason": reason,
        "source": n.source,
        "external_id": n.external_id,
        "source_url": n.source_url,
        "filled": {name: content[name] for name in filled},
    }


def upsert_event(
    session: Session,
    n: NormalizedEvent,
    *,
    crawl_run_id: int | None = None,
    city: str | None = None,
    fingerprint: str | None = None,
    now: datetime | None = None,
) -> UpsertResult:
    """Write one normalized event in one transaction (commits; rolls back on error).

    *city* is the registry slug the listing was discovered under; ``None``
    (a run over every city, or the refresh) keeps the one already stored.
    *fingerprint* is the listing item's fingerprint (``Discovered``), stored
    for :func:`skip_unchanged` on the next crawl; ``None`` (a plain URL, the
    refresh) keeps the one already stored.
    """
    now = now or datetime.now(UTC)
    try:
        result = _upsert(session, n, crawl_run_id, city, fingerprint, now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    log.info(
        "upsert.event",
        source=n.source,
        event_external_id=n.external_id,
        event_id=result.event_id,
        created=result.created,
        merged=result.merged,
        review=result.review,
        version_written=result.version_written,
        status=result.status.value,
    )
    return result


def _upsert(
    session: Session,
    n: NormalizedEvent,
    crawl_run_id: int | None,
    city: str | None,
    fingerprint: str | None,
    now: datetime,
) -> UpsertResult:
    source = get_or_create_source(session, n.source)
    category = get_or_create_category(session, n.category)
    organizer = get_or_create_organizer(session, n.organizer) if n.organizer else None
    venue = get_or_create_venue(session, n.venue) if n.venue else None

    listing = session.scalar(
        select(SourceListing).where(
            SourceListing.source_id == source.id, SourceListing.external_id == n.external_id
        )
    )
    created = merged = review = False
    ambiguous: list[Event] = []  # flagged once the new event has an id
    if listing is None:
        # First time this source shows the listing: is it an event we already have?
        found = match(session, n)
        if found.target is not None:
            event = found.target
            merged = True
        else:
            event = Event(status=EventStatus.SCHEDULED, content_hash="", first_seen_at=now)
            session.add(event)
            created = True
            ambiguous = found.candidates
        listing = SourceListing(
            source=source,
            event=event,
            external_id=n.external_id,
            source_url=n.source_url,
            content_hash="",
            first_seen_at=now,
        )
        session.add(listing)
    else:
        event = listing.event
        # The listing keeps its event; a look-alike that appeared since is for a human.
        review = bool(flag_duplicates(session, n, event.id))

    if created or (not merged and _is_primary(event, listing)):
        current: EventStatus | None = None if created else event.status
        changed = event.content_hash != n.content_hash
        status = next_status(current, n.status, changed)
        version_written = created or changed or status != current
        has_secondaries = len(event.listings) > 1
        if created or changed:
            _apply_content(event, n, keep_filled=has_secondaries)
        if venue is not None or not has_secondaries:
            event.venue = venue
        if organizer is not None or not has_secondaries:
            event.organizer = organizer
        if category.slug != UNCATEGORIZED or not has_secondaries:
            event.category = category
        content = _version_content(n, status)
    else:
        # Secondary listing (ADR 0010): first seen wins, empty fields filled from any source.
        listing_changed = listing.content_hash != n.content_hash
        status = event.status
        if status is EventStatus.REMOVED_FROM_SOURCE and n.status is EventStatus.SCHEDULED:
            status = EventStatus.SCHEDULED  # another source still lists it
        filled = (
            _fill_empty(event, n, venue, organizer, category) if merged or listing_changed else []
        )
        version_written = merged or bool(filled) or status != event.status
        reason = "listing merged" if merged else "listing filled"
        content = _merge_content(n, status, reason, filled)
        if merged:
            log.info(
                "dedupe.merged",
                source=n.source,
                event_external_id=n.external_id,
                event_id=event.id,
                filled=filled,
            )

    event.status = status
    event.last_seen_at = now
    if version_written and not created:
        event.version += 1
    session.flush()
    if ambiguous:
        flag_for_review(session, n, event, ambiguous)
        review = True

    if version_written:
        session.add(
            EventVersion(
                event_id=event.id,
                version=event.version,
                status=status,
                content_hash=event.content_hash,
                content=content,
                created_at=now,
            )
        )

    listing.source_url = n.source_url
    listing.content_hash = n.content_hash
    if fingerprint is not None:
        listing.listing_fingerprint = fingerprint
    listing.last_seen_at = now
    listing.last_crawl_run_id = crawl_run_id
    listing.removed_at = None  # seen again: back on its source (the status above revived the event)
    if city is not None:
        listing.city = city
    session.flush()

    snapshot_written = False
    if n.raw:
        sha, blob = snapshot_of(n.raw)
        exists = session.scalar(
            select(RawSnapshot.id).where(
                RawSnapshot.source_listing_id == listing.id, RawSnapshot.sha256 == sha
            )
        )
        if exists is None:
            session.add(
                RawSnapshot(
                    source_listing_id=listing.id, sha256=sha, payload_gzip=blob, fetched_at=now
                )
            )
            snapshot_written = True
    session.flush()
    return UpsertResult(
        event.id,
        listing.id,
        created,
        version_written,
        snapshot_written,
        status,
        merged=merged,
        review=review,
    )


def skip_unchanged(
    session: Session,
    source_name: str,
    external_id: str,
    fingerprint: str,
    *,
    city: str | None,
    crawl_run_id: int | None,
    within: timedelta,
    now: datetime | None = None,
) -> bool:
    """Count a listing as seen without fetching it, if nothing about it can have changed.

    True (and the listing stamped, committed) only when the stored
    ``listing_fingerprint`` equals *fingerprint*, the listing is not removed,
    its event is active and it starts later than *within* from now: an event
    about to start is always re-fetched (the refresh window), and a removed
    listing or event is re-fetched so the upsert can revive it. False means
    fetch as usual; nothing is written then. The stamp is the one an upsert
    makes — ``last_seen_at``, ``last_crawl_run_id``, *city* when given — so
    :func:`mark_missing` treats the listing as seen.
    """
    now = now or datetime.now(UTC)
    listing = session.scalar(
        select(SourceListing)
        .join(Source, SourceListing.source_id == Source.id)
        .options(joinedload(SourceListing.event))
        .where(Source.name == source_name, SourceListing.external_id == external_id)
    )
    if (
        listing is None
        or listing.listing_fingerprint != fingerprint
        or listing.removed_at is not None
        or listing.event.status not in ACTIVE
        or listing.event.starts_at <= now + within
    ):
        return False
    listing.last_seen_at = now
    listing.last_crawl_run_id = crawl_run_id
    if city is not None:
        listing.city = city
    session.commit()
    log.info(
        "upsert.listing_skipped",
        source=source_name,
        city=city,
        event_external_id=external_id,
        event_id=listing.event_id,
    )
    return True


def mark_missing(
    session: Session,
    source_name: str,
    city: str | None,
    seen_external_ids: set[str],
    *,
    now: datetime | None = None,
) -> int:
    """Stamp ``removed_at`` on listings of *source_name* in *city* that were not seen.

    Only call this after a complete discovery (no ``--limit``, no errors); a
    partial crawl must not remove what it did not look at. *city* scopes the
    sweep to the listings discovered under that registry slug; ``None`` means
    the run covered the whole source, so every listing of it is in scope
    (including those stored before listings carried a city).

    Only listings of active, upcoming events are considered. The event becomes
    ``removed_from_source``, with an ``event_versions`` row, when this leaves
    none of its listings on any source (ADR 0012); a merged event another
    source still lists stays as it is and gets no version. Returns the number
    of listings removed.
    """
    now = now or datetime.now(UTC)
    source = session.scalar(select(Source).where(Source.name == source_name))
    if source is None:
        return 0
    query = (
        select(SourceListing)
        .join(Event, SourceListing.event_id == Event.id)
        .where(
            SourceListing.source_id == source.id,
            SourceListing.removed_at.is_(None),
            Event.status.in_(ACTIVE),
            Event.starts_at > now,
        )
        .order_by(SourceListing.id)
    )
    if city is not None:
        query = query.where(SourceListing.city == city)
    listings = [x for x in session.scalars(query) if x.external_id not in seen_external_ids]
    return _retire(session, source_name, city, listings, now, "not seen on re-crawl")


def mark_gone(
    session: Session,
    source_name: str,
    urls: Iterable[str],
    *,
    external_ids: Iterable[str] = (),
    now: datetime | None = None,
) -> int:
    """Stamp ``removed_at`` on the listings whose own page *source_name* answered 404/410 for.

    A listing matches by its stored ``source_url`` or, for what discovery
    yielded, by ``external_id`` (Sympla stores the page's ``newUrl``, which
    may differ from the URL the crawl fetched).

    The source's own answer that the event is gone (ADR 0022); ``run_crawl``
    calls it only when the same run fetched or skipped more listings than it
    found gone.
    The scope and the event rule are ``mark_missing``'s: listings of active,
    upcoming events, and the event follows its last listing (ADR 0012).
    Returns the number of listings removed.
    """
    now = now or datetime.now(UTC)
    source = session.scalar(select(Source).where(Source.name == source_name))
    wanted, ids = set(urls), set(external_ids)
    if source is None or not (wanted or ids):
        return 0
    query = (
        select(SourceListing)
        .join(Event, SourceListing.event_id == Event.id)
        .where(
            SourceListing.source_id == source.id,
            or_(SourceListing.source_url.in_(wanted), SourceListing.external_id.in_(ids)),
            SourceListing.removed_at.is_(None),
            Event.status.in_(ACTIVE),
            Event.starts_at > now,
        )
        .order_by(SourceListing.id)
    )
    listings = list(session.scalars(query))
    return _retire(session, source_name, None, listings, now, "source page gone")


def _retire(
    session: Session,
    source_name: str,
    city: str | None,
    listings: list[SourceListing],
    now: datetime,
    reason: str,
) -> int:
    """Stamp *listings* removed; an event left with none becomes ``removed_from_source``."""
    events: dict[int, Event] = {}
    for listing in listings:
        listing.removed_at = now
        events[listing.event_id] = listing.event
        log.info(
            "upsert.listing_removed",
            source=source_name,
            city=city,
            event_external_id=listing.external_id,
            event_id=listing.event_id,
            reason=reason,
        )
    session.flush()
    for event in events.values():
        if any(listing.removed_at is None for listing in event.listings):
            continue  # another listing still has it
        event.status = EventStatus.REMOVED_FROM_SOURCE
        event.version += 1
        session.add(
            EventVersion(
                event_id=event.id,
                version=event.version,
                status=event.status,
                content_hash=event.content_hash,
                content={"status": event.status.value, "reason": reason},
                created_at=now,
            )
        )
        log.info("upsert.removed_from_source", source=source_name, city=city, event_id=event.id)
    session.commit()
    return len(listings)


__all__ = [
    "UpsertResult",
    "get_or_create_category",
    "get_or_create_organizer",
    "get_or_create_source",
    "get_or_create_venue",
    "mark_gone",
    "mark_missing",
    "next_status",
    "skip_unchanged",
    "snapshot_of",
    "upsert_event",
]
