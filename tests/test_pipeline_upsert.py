"""Upsert tests: pure helpers offline; idempotence and transitions on the scratch db."""

import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from evcrawl.db.models import (
    Event,
    EventStatus,
    EventVersion,
    Organizer,
    PriceLot,
    RawSnapshot,
    SourceListing,
    Venue,
)
from evcrawl.pipeline.normalize import normalize
from evcrawl.pipeline.upsert import (
    mark_missing,
    next_status,
    skip_unchanged,
    snapshot_of,
    upsert_event,
)
from evcrawl.sources.base import EventRecord
from evcrawl.sources.base import PriceLot as RecordLot

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def record(**overrides: Any) -> EventRecord:
    base: dict[str, Any] = {
        "source": "sympla",
        "external_id": "3537075",
        "source_url": "https://www.sympla.com.br/evento/pmi-ai-connect/3537075",
        "title": "PMI AI Connect",
        "starts_at": datetime(2026, 9, 26, 11, 0, tzinfo=UTC),
        "ends_at": datetime(2026, 9, 26, 16, 0, tzinfo=UTC),
        "venue_name": "[Unimed Vitória] Auditório Sede Administrativa",
        "address": "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES, 29050-662",
        "city": "Vitória",
        "state": "ES",
        "latitude": -20.3119792,
        "longitude": -40.3050745,
        "organizer": "PMI- ES",
        "category": "outro",
        "description": "Primeiro evento do PMI ES sobre IA.",
        "image_url": "https://images.sympla.com.br/6a8eea0e59ee2.png",
        "price_lots": [
            RecordLot(name="Público Geral - Lote 1", amount=Decimal("140.40")),
            RecordLot(name="Filiado - Lote 1", amount=Decimal("108.00")),
        ],
        "raw": {"event": {"id": 3537075, "detail": "<p>html</p>"}},
    }
    base.update(overrides)
    return EventRecord(**base)


# --- pure helpers


@pytest.mark.parametrize(
    ("current", "incoming", "changed", "expected"),
    [
        (None, EventStatus.SCHEDULED, True, EventStatus.SCHEDULED),
        (None, EventStatus.CANCELLED, True, EventStatus.CANCELLED),
        (EventStatus.SCHEDULED, EventStatus.SCHEDULED, False, EventStatus.SCHEDULED),
        (EventStatus.SCHEDULED, EventStatus.SCHEDULED, True, EventStatus.UPDATED),
        (EventStatus.UPDATED, EventStatus.SCHEDULED, False, EventStatus.UPDATED),
        (EventStatus.UPDATED, EventStatus.CANCELLED, False, EventStatus.CANCELLED),
        (EventStatus.CANCELLED, EventStatus.SCHEDULED, True, EventStatus.UPDATED),
        (EventStatus.SCHEDULED, EventStatus.ENDED, False, EventStatus.ENDED),
        (EventStatus.REMOVED_FROM_SOURCE, EventStatus.SCHEDULED, False, EventStatus.SCHEDULED),
        (EventStatus.REMOVED_FROM_SOURCE, EventStatus.SCHEDULED, True, EventStatus.UPDATED),
    ],
)
def test_next_status(
    current: EventStatus | None, incoming: EventStatus, changed: bool, expected: EventStatus
) -> None:
    assert next_status(current, incoming, changed) is expected


def test_snapshot_is_deterministic_and_round_trips() -> None:
    raw: dict[str, object] = {"b": 1, "a": {"detail": "<p>x</p>", "n": Decimal("1.5")}}
    sha1, blob1 = snapshot_of(raw)
    sha2, blob2 = snapshot_of({"a": {"n": Decimal("1.5"), "detail": "<p>x</p>"}, "b": 1})
    assert sha1 == sha2 and blob1 == blob2 and len(sha1) == 64
    assert json.loads(gzip.decompress(blob1)) == {"a": {"detail": "<p>x</p>", "n": "1.5"}, "b": 1}


# --- scratch database


@pytest.fixture
def session(scratch_engine: Engine) -> Session:
    with Session(scratch_engine) as s:
        yield s


def counts(session: Session) -> dict[str, int]:
    return {
        "events": session.scalar(select(func.count(Event.id))) or 0,
        "versions": session.scalar(select(func.count(EventVersion.id))) or 0,
        "listings": session.scalar(select(func.count(SourceListing.id))) or 0,
        "snapshots": session.scalar(select(func.count(RawSnapshot.id))) or 0,
        "lots": session.scalar(select(func.count(PriceLot.id))) or 0,
    }


def test_upsert_is_idempotent(session: Session) -> None:
    first = upsert_event(session, normalize(record(), now=NOW), now=NOW)
    assert first.created and first.version_written and first.snapshot_written
    assert first.status is EventStatus.SCHEDULED
    after_first = counts(session)
    assert after_first == {"events": 1, "versions": 1, "listings": 1, "snapshots": 1, "lots": 2}

    second = upsert_event(session, normalize(record(), now=NOW), now=NOW + timedelta(hours=6))
    assert second.event_id == first.event_id and second.listing_id == first.listing_id
    assert not second.created and not second.version_written and not second.snapshot_written
    assert counts(session) == after_first  # second run: 0 new versions

    event = session.get(Event, first.event_id)
    assert event is not None
    assert event.version == 1 and event.status is EventStatus.SCHEDULED
    assert event.last_seen_at == NOW + timedelta(hours=6)
    assert event.venue is not None and event.venue.city == "Vitória"
    assert event.organizer is not None and event.organizer.name == "PMI- ES"
    assert event.category is not None and event.category.slug == "uncategorized"
    assert [str(lot.amount) for lot in sorted(event.price_lots, key=lambda p: p.position)] == [
        "140.40",
        "108.00",
    ]
    listing = session.get(SourceListing, first.listing_id)
    assert listing is not None and listing.source_url == str(record().source_url)
    within = session.execute(
        text(
            "select count(*) from venues where ST_DWithin(location, "
            "ST_GeogFromText('SRID=4326;POINT(-40.3050745 -20.3119792)'), 5)"
        )
    ).scalar_one()
    assert within == 1


def test_content_change_writes_a_version_and_marks_updated(session: Session) -> None:
    first = upsert_event(session, normalize(record(), now=NOW), now=NOW)
    changed = upsert_event(
        session,
        normalize(record(title="PMI AI Connect 2026", raw={"event": {"id": 1, "v": 2}}), now=NOW),
        now=NOW,
    )
    assert changed.event_id == first.event_id
    assert changed.version_written and changed.snapshot_written
    assert changed.status is EventStatus.UPDATED
    event = session.get(Event, first.event_id)
    assert event is not None and event.version == 2 and event.title == "PMI AI Connect 2026"
    versions = session.scalars(
        select(EventVersion).where(EventVersion.event_id == event.id).order_by(EventVersion.version)
    ).all()
    assert [v.version for v in versions] == [1, 2]
    assert [v.status for v in versions] == [EventStatus.SCHEDULED, EventStatus.UPDATED]
    assert versions[1].content["title"] == "PMI AI Connect 2026"
    assert counts(session)["snapshots"] == 2

    # same payload again: the identical snapshot is not stored twice
    again = upsert_event(
        session,
        normalize(record(title="PMI AI Connect 2026", raw={"event": {"id": 1, "v": 2}}), now=NOW),
        now=NOW,
    )
    assert not again.version_written and not again.snapshot_written
    assert counts(session)["snapshots"] == 2


def test_cancelled_then_removed_then_back(session: Session) -> None:
    first = upsert_event(session, normalize(record(), now=NOW), now=NOW)
    cancelled = upsert_event(session, normalize(record(cancelled=True), now=NOW), now=NOW)
    assert cancelled.status is EventStatus.CANCELLED and cancelled.version_written

    # a source that un-cancels: content hash changes back -> updated
    back = upsert_event(session, normalize(record(), now=NOW), now=NOW)
    assert back.status is EventStatus.UPDATED

    assert mark_missing(session, "sympla", None, set(), now=NOW) == 1
    event = session.get(Event, first.event_id)
    assert event is not None and event.status is EventStatus.REMOVED_FROM_SOURCE
    assert mark_missing(session, "sympla", None, set(), now=NOW) == 0  # already inactive

    seen_again = upsert_event(session, normalize(record(), now=NOW), now=NOW)
    assert seen_again.status is EventStatus.SCHEDULED and seen_again.version_written
    assert counts(session)["events"] == 1
    assert counts(session)["versions"] == 5


def test_mark_missing_spares_seen_and_past_events(session: Session) -> None:
    upsert_event(session, normalize(record(), now=NOW), now=NOW)
    upsert_event(
        session,
        normalize(
            record(external_id="2", source_url="https://www.sympla.com.br/evento/b/2", title="B"),
            now=NOW,
        ),
        now=NOW,
    )
    past = record(
        external_id="3",
        source_url="https://www.sympla.com.br/evento/c/3",
        title="C",
        starts_at=NOW - timedelta(days=2),
        ends_at=NOW - timedelta(days=1),
    )
    upsert_event(session, normalize(past, now=NOW), now=NOW)  # already 'ended'
    assert mark_missing(session, "sympla", None, {"3537075"}, now=NOW) == 1
    statuses = dict(
        session.execute(select(SourceListing.external_id, Event.status).join(Event)).all()
    )
    assert statuses == {
        "3537075": EventStatus.SCHEDULED,
        "2": EventStatus.REMOVED_FROM_SOURCE,
        "3": EventStatus.ENDED,
    }


def test_upsert_keeps_the_stored_fingerprint_unless_given_one(session: Session) -> None:
    n = normalize(record(), now=NOW)
    listing_id = upsert_event(session, n, fingerprint="fp1", now=NOW).listing_id
    assert session.get_one(SourceListing, listing_id).listing_fingerprint == "fp1"
    upsert_event(session, n, now=NOW)  # a refresh: plain URL, no fingerprint
    assert session.get_one(SourceListing, listing_id).listing_fingerprint == "fp1"
    upsert_event(session, n, fingerprint="fp2", now=NOW)
    assert session.get_one(SourceListing, listing_id).listing_fingerprint == "fp2"


def test_skip_unchanged_only_when_nothing_can_have_changed(session: Session) -> None:
    within = timedelta(hours=48)
    later = NOW + timedelta(hours=1)  # the event starts 2026-09-26: well outside the window

    def skip(fingerprint: str, *, now: datetime = later, city: str | None = "vitoria") -> bool:
        return skip_unchanged(
            session,
            "sympla",
            "3537075",
            fingerprint,
            city=city,
            crawl_run_id=None,
            within=within,
            now=now,
        )

    assert not skip("fp1")  # no listing yet
    n = normalize(record(), now=NOW)
    listing_id = upsert_event(session, n, fingerprint="fp1", now=NOW).listing_id
    assert not skip("fp2")  # the listing item changed
    assert not skip("fp1", now=datetime(2026, 9, 25, 12, 0, tzinfo=UTC))  # starts within 48 h
    assert counts(session)["versions"] == 1
    listing = session.get_one(SourceListing, listing_id)
    assert listing.last_seen_at == NOW and listing.city is None  # nothing was written

    assert skip("fp1")
    session.refresh(listing)
    assert listing.last_seen_at == later and listing.city == "vitoria"
    assert counts(session)["versions"] == 1  # a skip writes no version
    assert skip("fp1", city=None) and listing.city == "vitoria"  # None keeps the stored city

    assert mark_missing(session, "sympla", None, set(), now=later) == 1
    assert not skip("fp1")  # removed: fetch it so the upsert can revive the event
    upsert_event(session, normalize(record(cancelled=True), now=NOW), fingerprint="fp1", now=later)
    assert not skip("fp1")  # back, but cancelled: not an active event


def test_shared_venue_and_organizer_rows(session: Session) -> None:
    upsert_event(session, normalize(record(), now=NOW), now=NOW)
    upsert_event(
        session,
        normalize(
            record(
                external_id="9",
                source_url="https://www.sympla.com.br/evento/z/9",
                title="Z",
                latitude=None,
                longitude=None,
            ),
            now=NOW,
        ),
        now=NOW,
    )
    assert session.scalar(select(func.count(Venue.id))) == 1
    assert session.scalar(select(func.count(Organizer.id))) == 1
    venue = session.scalar(select(Venue))
    assert venue is not None and venue.location is not None  # coordinates never blanked
