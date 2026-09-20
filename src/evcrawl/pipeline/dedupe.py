"""Cross-source matching scaffold (AGENTS.md §7, spec task 7).

Matching keys: normalized title + start within ±2 h + venue within 300 m.
Phase 1 only finds candidates and flags both sides with
``events.dedupe_review = true``; merging listings into one canonical event is
Phase 2.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from evcrawl.db.models import Event, Venue
from evcrawl.logging import get_logger
from evcrawl.pipeline.normalize import NormalizedEvent

log = get_logger("evcrawl.pipeline.dedupe")

START_TOLERANCE = timedelta(hours=2)
VENUE_RADIUS_M = 300


def match_key(title_normalized: str, starts_at: datetime) -> str:
    """Cheap bucketing key: normalized title + UTC calendar day of the start.

    Two listings of one event always share it unless the ±2 h tolerance crosses
    midnight UTC; the SQL in :func:`find_candidates` is the real test.
    """
    return f"{title_normalized}|{starts_at.strftime('%Y-%m-%d')}"


def find_candidates(
    session: Session, n: NormalizedEvent, *, exclude_event_id: int | None = None
) -> list[Event]:
    """Events that look like *n*: same normalized title, start ±2 h, venue within 300 m.

    With no coordinates on either side the venue test falls back to the same
    normalized address. Online events match on title and time alone.
    """
    query = (
        select(Event)
        .where(
            Event.title_normalized == n.title_normalized,
            Event.starts_at >= n.starts_at - START_TOLERANCE,
            Event.starts_at <= n.starts_at + START_TOLERANCE,
        )
        .order_by(Event.id)
    )
    if exclude_event_id is not None:
        query = query.where(Event.id != exclude_event_id)
    venue = n.venue
    if venue is not None:
        query = query.join(Venue, Event.venue_id == Venue.id)
        if venue.latitude is not None and venue.longitude is not None:
            point = func.ST_GeogFromText(f"SRID=4326;POINT({venue.longitude} {venue.latitude})")
            query = query.where(func.ST_DWithin(Venue.location, point, VENUE_RADIUS_M))
        else:
            query = query.where(Venue.address_normalized == venue.address_normalized)
    return list(session.scalars(query).all())


def flag_duplicates(session: Session, n: NormalizedEvent, event_id: int) -> list[int]:
    """Flag *event_id* and every candidate for review; returns the candidate ids.

    Nothing is merged: an ambiguous match keeps the listings separate with
    ``dedupe_review = true`` (AGENTS.md §7). Commits.
    """
    candidates = find_candidates(session, n, exclude_event_id=event_id)
    if not candidates:
        return []
    event = session.get(Event, event_id)
    if event is not None:
        event.dedupe_review = True
    for candidate in candidates:
        candidate.dedupe_review = True
    session.commit()
    ids = [c.id for c in candidates]
    log.warning(
        "dedupe.review",
        source=n.source,
        event_external_id=n.external_id,
        event_id=event_id,
        candidates=ids,
        match_key=match_key(n.title_normalized, n.starts_at),
    )
    return ids


__all__ = ["START_TOLERANCE", "VENUE_RADIUS_M", "find_candidates", "flag_duplicates", "match_key"]
