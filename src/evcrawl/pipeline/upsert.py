"""Writing events to the database with versioning (AGENTS.md §7, spec task 6).

``upsert_event`` runs one transaction per event: get-or-create the venue,
organizer and category, upsert the ``source_listings`` row, insert or update
the canonical ``events`` row with its status transition, append an
``event_versions`` row only when the normalized content hash (or the status)
changed, and store a gzip of the source payload in ``raw_snapshots`` once per
distinct payload. Nothing is ever deleted; ``mark_missing`` flips listings that
a full crawl did not see to ``removed_from_source``.
"""

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from geoalchemy2 import WKTElement
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from evcrawl.pipeline.normalize import (
    CATEGORIES,
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
    created: bool
    version_written: bool
    snapshot_written: bool
    status: EventStatus


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


def _apply_content(event: Event, n: NormalizedEvent) -> None:
    event.title = n.title
    event.title_normalized = n.title_normalized
    event.description = n.description
    event.starts_at = n.starts_at
    event.ends_at = n.ends_at
    event.timezone = n.timezone
    event.is_online = n.is_online
    event.image_url = n.image_url
    event.content_hash = n.content_hash
    event.price_lots = [
        PriceLot(name=lot.name, amount=lot.amount, currency=lot.currency, position=i)
        for i, lot in enumerate(n.price_lots)
    ]


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


def upsert_event(
    session: Session,
    n: NormalizedEvent,
    *,
    crawl_run_id: int | None = None,
    now: datetime | None = None,
) -> UpsertResult:
    """Write one normalized event in one transaction (commits; rolls back on error)."""
    now = now or datetime.now(UTC)
    try:
        result = _upsert(session, n, crawl_run_id, now)
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
        version_written=result.version_written,
        status=result.status.value,
    )
    return result


def _upsert(
    session: Session, n: NormalizedEvent, crawl_run_id: int | None, now: datetime
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
    created = listing is None
    if listing is None:
        event = Event(status=EventStatus.SCHEDULED, content_hash="", first_seen_at=now)
        session.add(event)
        listing = SourceListing(
            source=source,
            event=event,
            external_id=n.external_id,
            source_url=n.source_url,
            content_hash="",
            first_seen_at=now,
        )
        session.add(listing)
        current: EventStatus | None = None
    else:
        event = listing.event
        current = event.status

    changed = event.content_hash != n.content_hash
    status = next_status(current, n.status, changed)
    version_written = created or changed or status != current

    if created or changed:
        _apply_content(event, n)
    event.venue = venue
    event.organizer = organizer
    event.category = category
    event.status = status
    event.last_seen_at = now
    if version_written and not created:
        event.version += 1
    session.flush()

    if version_written:
        session.add(
            EventVersion(
                event_id=event.id,
                version=event.version,
                status=status,
                content_hash=n.content_hash,
                content=_version_content(n, status),
                created_at=now,
            )
        )

    listing.source_url = n.source_url
    listing.content_hash = n.content_hash
    listing.last_seen_at = now
    listing.last_crawl_run_id = crawl_run_id
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
    return UpsertResult(event.id, listing.id, created, version_written, snapshot_written, status)


def mark_missing(
    session: Session,
    source_name: str,
    seen_external_ids: set[str],
    *,
    now: datetime | None = None,
) -> int:
    """Mark active, upcoming events of *source_name* not in *seen_external_ids* as removed.

    Only call this after a full discovery of the source (no ``--limit``, all
    cities); a partial crawl must not remove what it did not look at. Returns
    the number of events marked; each gets an ``event_versions`` row.
    """
    now = now or datetime.now(UTC)
    source = session.scalar(select(Source).where(Source.name == source_name))
    if source is None:
        return 0
    rows = session.execute(
        select(SourceListing, Event)
        .join(Event, SourceListing.event_id == Event.id)
        .where(
            SourceListing.source_id == source.id,
            Event.status.in_(ACTIVE),
            Event.starts_at > now,
        )
    ).all()
    marked = 0
    for listing, event in rows:
        if listing.external_id in seen_external_ids:
            continue
        event.status = EventStatus.REMOVED_FROM_SOURCE
        event.version += 1
        session.add(
            EventVersion(
                event_id=event.id,
                version=event.version,
                status=event.status,
                content_hash=event.content_hash,
                content={"status": event.status.value, "reason": "not seen on re-crawl"},
                created_at=now,
            )
        )
        marked += 1
        log.info(
            "upsert.removed_from_source",
            source=source_name,
            event_external_id=listing.external_id,
            event_id=event.id,
        )
    session.commit()
    return marked


__all__ = [
    "UpsertResult",
    "get_or_create_category",
    "get_or_create_organizer",
    "get_or_create_source",
    "get_or_create_venue",
    "mark_missing",
    "next_status",
    "snapshot_of",
    "upsert_event",
]
