"""Cross-source matching (AGENTS.md §7, Phase 2 task 9, ADR 0010).

Matching keys: normalized title + start within ±2 h + venue within 300 m.
:func:`match` turns the candidates into a decision for a listing the upsert
has not seen before: link it to the one confident candidate, or keep it
separate and flag every side with ``events.dedupe_review = true`` when the
match is ambiguous. Which fields the merged row takes from which listing is
the upsert's business (``pipeline/upsert.py``, ADR 0010).
"""

from dataclasses import dataclass, field
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


def is_confident(candidate: Event, n: NormalizedEvent) -> bool:
    """Whether *candidate* may take *n*'s listing without a human look (ADR 0010).

    Two things make a candidate unsure: it already carries a listing from
    ``n.source`` (the same platform listing one event twice is a duplicate
    posting or a second session, not a cross-source match), or it is already
    under review because of some other event.
    """
    if candidate.dedupe_review:
        return False
    return all(listing.source.name != n.source for listing in candidate.listings)


@dataclass(frozen=True)
class Match:
    """What the events table says about a listing it has not seen before."""

    target: Event | None = None  # the one confident candidate; link the listing to it
    candidates: list[Event] = field(default_factory=list)  # every candidate, confident or not

    @property
    def review(self) -> bool:
        """Keep the listing separate and flag: candidates exist but none is the one."""
        return self.target is None and bool(self.candidates)


def match(session: Session, n: NormalizedEvent) -> Match:
    """Decide for a new listing: no candidate, one confident candidate, or review.

    Exactly one candidate that :func:`is_confident` becomes the target; any
    other outcome (several candidates, or one that is unsure) is a review.
    """
    candidates = find_candidates(session, n)
    if len(candidates) == 1 and is_confident(candidates[0], n):
        return Match(target=candidates[0], candidates=candidates)
    return Match(candidates=candidates)


def flag_for_review(
    session: Session, n: NormalizedEvent, event: Event, candidates: list[Event]
) -> list[int]:
    """Flag *event* and every candidate; returns the candidate ids. Does not commit.

    Nothing is merged: an ambiguous match keeps the listings separate with
    ``dedupe_review = true`` (AGENTS.md §7).
    """
    if not candidates:
        return []
    event.dedupe_review = True
    for candidate in candidates:
        candidate.dedupe_review = True
    ids = [c.id for c in candidates]
    log.warning(
        "dedupe.review",
        source=n.source,
        event_external_id=n.external_id,
        event_id=event.id,
        candidates=ids,
        match_key=match_key(n.title_normalized, n.starts_at),
    )
    return ids


def flag_duplicates(session: Session, n: NormalizedEvent, event_id: int) -> list[int]:
    """Re-crawl path: flag *event_id* and whatever else now looks like it. Does not commit.

    A listing that already has its event is never moved to another one; if a
    look-alike appeared since, both sides get ``dedupe_review`` for a human.
    """
    event = session.get(Event, event_id)
    if event is None:
        return []
    return flag_for_review(
        session, n, event, find_candidates(session, n, exclude_event_id=event_id)
    )


__all__ = [
    "START_TOLERANCE",
    "VENUE_RADIUS_M",
    "Match",
    "find_candidates",
    "flag_duplicates",
    "flag_for_review",
    "is_confident",
    "match",
    "match_key",
]
