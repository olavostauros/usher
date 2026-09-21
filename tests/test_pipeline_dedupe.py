"""Dedupe: match_key offline; candidate SQL, the merge rule and its edge cases on the scratch db.

The two-source records below mirror what the Sympla and uTicket adapters
return for one and the same event (``tests/fixtures/http/sympla/event-*.html``,
``tests/fixtures/http/uticket/event-*.json``): the same title with different
casing, the same start, coordinates ~100 m apart, one side with a description,
image and real price lots, the other with ``A partir de`` and nothing else.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from evcrawl.db.models import Event, EventStatus, EventVersion, SourceListing
from evcrawl.pipeline.dedupe import (
    find_candidates,
    flag_duplicates,
    is_confident,
    match,
    match_key,
)
from evcrawl.pipeline.normalize import NormalizedEvent, normalize
from evcrawl.pipeline.upsert import mark_missing, upsert_event
from evcrawl.sources.base import EventRecord, PriceLot

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 1, 23, 0, tzinfo=UTC)
# Praça do Papa, Vitória; ~100 m and ~5 km away
HERE = (-20.3172, -40.2929)
NEAR = (-20.3181, -40.2929)
FAR = (-20.2774, -40.3022)


def record(external_id: str, **overrides: Any) -> EventRecord:
    base: dict[str, Any] = {
        "source": "sympla",
        "external_id": external_id,
        "source_url": f"https://www.sympla.com.br/evento/x/{external_id}",
        "title": "Forró da Praça",
        "starts_at": START,
        "venue_name": "Praça do Papa",
        "address": f"Praça do Papa, s/n, Enseada do Suá, Vitória - ES ({external_id})",
        "city": "Vitória",
        "state": "ES",
        "latitude": HERE[0],
        "longitude": HERE[1],
    }
    base.update(overrides)
    return EventRecord(**base)


def sympla_record(**overrides: Any) -> EventRecord:
    """What ``SymplaAdapter.fetch`` gives: full record, real lots, coordinates."""
    base: dict[str, Any] = {
        "source": "sympla",
        "external_id": "3600001",
        "source_url": "https://www.sympla.com.br/evento/eclipse-festival/3600001",
        "title": "Eclipse Festival",
        "starts_at": datetime(2026, 10, 31, 18, 0, tzinfo=UTC),
        "ends_at": datetime(2026, 11, 1, 19, 0, tzinfo=UTC),
        "venue_name": "Sítio Marinho",
        "address": "Rodovia ES-388, km 12, Jardim Vila Velha, Vila Velha - ES, 29120-000",
        "city": "Vila Velha",
        "state": "ES",
        "latitude": -20.477737,
        "longitude": -40.4297,
        "organizer": "Produtora Exemplo",
        "category": "festas-e-shows",
        "description": "A Eclipse Festival é um evento de música eletrônica ao ar livre.",
        "image_url": "https://images.sympla.com.br/eclipse.png",
        "price_lots": [
            PriceLot(name="1º lote", amount=Decimal("80.00")),
            PriceLot(name="2º lote", amount=Decimal("100.00")),
        ],
        "raw": {"event": {"id": 3600001}},
    }
    base.update(overrides)
    return EventRecord(**base)


def uticket_record(**overrides: Any) -> EventRecord:
    """What ``UTicketAdapter.fetch`` gives for the same event: less, and ~100 m off."""
    base: dict[str, Any] = {
        "source": "uticket",
        "external_id": "01M4IIJK3JGTH9",
        "source_url": "https://uticket.com.br/event/01M4IIJK3JGTH9",
        "title": "ECLIPSE FESTIVAL",
        "starts_at": datetime(2026, 10, 31, 18, 0, tzinfo=UTC),
        "venue_name": "Sítio Marinho",
        "address": "Jardim vila velha - Vila Velha-ES",
        "city": "Vila Velha",
        "state": "ES",
        "latitude": -20.4786,
        "longitude": -40.4297,
        "price_lots": [PriceLot(name="A partir de", amount=Decimal("80.00"))],
        "raw": {"id": "01M4IIJK3JGTH9", "price_lots_pending": True},
    }
    base.update(overrides)
    return EventRecord(**base)


# --- match_key


@pytest.mark.parametrize(
    ("title", "starts_at", "expected"),
    [
        ("forro da praca", START, "forro da praca|2026-10-01"),
        ("forro da praca", START + timedelta(minutes=59), "forro da praca|2026-10-01"),
        ("forro da praca", START + timedelta(hours=2), "forro da praca|2026-10-02"),
        ("", START, "|2026-10-01"),
    ],
)
def test_match_key(title: str, starts_at: datetime, expected: str) -> None:
    assert match_key(title, starts_at) == expected


def test_match_key_of_normalized_titles_is_accent_insensitive() -> None:
    a = normalize(record("1", title="Forró da Praça"), now=NOW)
    b = normalize(record("2", title="  FORRO DA PRACA "), now=NOW)
    assert match_key(a.title_normalized, a.starts_at) == match_key(b.title_normalized, b.starts_at)


# --- scratch database


@pytest.fixture
def session(scratch_engine: Engine) -> Session:
    with Session(scratch_engine) as s:
        yield s


def counts(session: Session) -> dict[str, int]:
    return {
        "events": session.scalar(select(func.count(Event.id))) or 0,
        "listings": session.scalar(select(func.count(SourceListing.id))) or 0,
        "versions": session.scalar(select(func.count(EventVersion.id))) or 0,
        "review": session.scalar(select(func.count(Event.id)).where(Event.dedupe_review)) or 0,
    }


def sources_of(session: Session, event_id: int) -> list[str]:
    rows = session.execute(
        select(SourceListing)
        .where(SourceListing.event_id == event_id)
        .order_by(SourceListing.first_seen_at, SourceListing.id)
    ).scalars()
    return [listing.source.name for listing in rows]


def test_find_candidates_title_time_and_distance(session: Session) -> None:
    here = upsert_event(session, normalize(record("1"), now=NOW), now=NOW)
    near = normalize(
        record("2", starts_at=START + timedelta(hours=1), latitude=NEAR[0], longitude=NEAR[1]),
        now=NOW,
    )
    far = normalize(record("3", latitude=FAR[0], longitude=FAR[1]), now=NOW)
    late = normalize(record("4", starts_at=START + timedelta(hours=3)), now=NOW)
    other_title = normalize(record("5", title="Samba da Praça"), now=NOW)

    assert [e.id for e in find_candidates(session, near)] == [here.event_id]
    assert find_candidates(session, far) == []
    assert find_candidates(session, late) == []
    assert find_candidates(session, other_title) == []
    same = normalize(record("1"), now=NOW)
    assert find_candidates(session, same, exclude_event_id=here.event_id) == []


def test_find_candidates_without_coordinates_uses_the_address(session: Session) -> None:
    here = upsert_event(session, normalize(record("1"), now=NOW), now=NOW)
    same_address = normalize(
        record("2", address=record("1").address, latitude=None, longitude=None), now=NOW
    )
    other_address = normalize(record("3", latitude=None, longitude=None), now=NOW)
    assert [e.id for e in find_candidates(session, same_address)] == [here.event_id]
    assert find_candidates(session, other_address) == []


# --- the decision (ADR 0010)


def test_match_one_confident_candidate_is_the_target(session: Session) -> None:
    first = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    n = normalize(uticket_record(), now=NOW)
    found = match(session, n)
    assert found.target is not None and found.target.id == first.event_id
    assert not found.review


def test_match_nothing(session: Session) -> None:
    upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    found = match(session, normalize(uticket_record(title="Outro Festival"), now=NOW))
    assert found.target is None and found.candidates == [] and not found.review


def test_same_source_candidate_is_not_confident(session: Session) -> None:
    first = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    event = session.get_one(Event, first.event_id)
    n = normalize(sympla_record(external_id="3600002"), now=NOW)
    assert not is_confident(event, n)
    found = match(session, n)
    assert found.target is None and found.review


def test_candidate_under_review_is_not_confident(session: Session) -> None:
    first = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    event = session.get_one(Event, first.event_id)
    event.dedupe_review = True
    session.flush()
    n: NormalizedEvent = normalize(uticket_record(), now=NOW)
    assert not is_confident(event, n)
    assert match(session, n).review


def test_flag_duplicates_marks_both_sides_and_merges_nothing(session: Session) -> None:
    first = upsert_event(session, normalize(record("1"), now=NOW), now=NOW)
    assert flag_duplicates(session, normalize(record("1"), now=NOW), first.event_id) == []

    # a same-source look-alike: kept separate and flagged, never merged
    near = normalize(
        record("2", starts_at=START + timedelta(hours=1), latitude=NEAR[0], longitude=NEAR[1]),
        now=NOW,
    )
    second = upsert_event(session, near, now=NOW)
    assert second.created and second.review and not second.merged
    assert flag_duplicates(session, near, second.event_id) == [first.event_id]

    flags = dict(session.execute(select(Event.id, Event.dedupe_review)).all())
    assert flags == {first.event_id: True, second.event_id: True}


# --- merge live (spec task 9 done-criteria)


def test_same_event_from_two_sources_becomes_one_event(session: Session) -> None:
    first = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    second = upsert_event(session, normalize(uticket_record(), now=NOW + timedelta(hours=1)))
    assert second.event_id == first.event_id
    assert second.merged and not second.created and not second.review
    assert second.version_written and second.listing_id != first.listing_id
    assert counts(session) == {"events": 1, "listings": 2, "versions": 2, "review": 0}
    assert sources_of(session, first.event_id) == ["sympla", "uticket"]

    # first seen wins: Sympla's title, description, image, venue and lots stand
    event = session.get_one(Event, first.event_id)
    assert event.title == "Eclipse Festival"
    assert event.description is not None and event.description.startswith("A Eclipse")
    assert event.image_url == "https://images.sympla.com.br/eclipse.png"
    assert event.venue is not None and event.venue.name == "Sítio Marinho"
    assert event.organizer is not None and event.organizer.name == "Produtora Exemplo"
    assert event.category is not None and event.category.slug == "party"
    assert [str(lot.amount) for lot in event.price_lots] == ["80.00", "100.00"]
    assert event.version == 2 and event.status is EventStatus.SCHEDULED
    assert event.first_seen_at == NOW

    version = session.scalars(
        select(EventVersion).where(EventVersion.event_id == event.id, EventVersion.version == 2)
    ).one()
    assert version.content["reason"] == "listing merged"
    assert version.content["source"] == "uticket"
    assert version.content["filled"] == {}

    # the second listing is attributed to its own source (AGENTS.md §10)
    listing = session.get_one(SourceListing, second.listing_id)
    assert listing.source_url == "https://uticket.com.br/event/01M4IIJK3JGTH9"


def test_second_run_of_both_sources_is_idempotent(session: Session) -> None:
    upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    upsert_event(session, normalize(uticket_record(), now=NOW), now=NOW)
    before = counts(session)
    later = NOW + timedelta(hours=6)
    a = upsert_event(session, normalize(sympla_record(), now=later), now=later)
    b = upsert_event(session, normalize(uticket_record(), now=later), now=later)
    assert not a.version_written and not a.merged and not a.created and not a.review
    assert not b.version_written and not b.merged and not b.created and not b.review
    assert counts(session) == before
    event = session.get_one(Event, a.event_id)
    assert event.version == 2 and event.last_seen_at == later


def test_secondary_fills_only_what_the_primary_lacks(session: Session) -> None:
    # uTicket first, with less; Sympla second, with everything
    first = upsert_event(session, normalize(uticket_record(), now=NOW), now=NOW)
    second = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    assert second.merged and second.event_id == first.event_id
    event = session.get_one(Event, first.event_id)
    assert event.title == "ECLIPSE FESTIVAL"  # first seen wins
    assert [lot.name for lot in event.price_lots] == ["A partir de"]  # not empty: kept
    assert event.venue is not None and event.venue.name == "Sítio Marinho"
    # empty fields filled from the second source
    assert event.description is not None and event.description.startswith("A Eclipse")
    assert event.image_url == "https://images.sympla.com.br/eclipse.png"
    assert event.ends_at == datetime(2026, 11, 1, 19, 0, tzinfo=UTC)
    assert event.organizer is not None and event.organizer.name == "Produtora Exemplo"
    assert event.category is not None and event.category.slug == "party"
    version = session.scalars(
        select(EventVersion).where(EventVersion.event_id == event.id, EventVersion.version == 2)
    ).one()
    assert sorted(version.content["filled"]) == [
        "category",
        "description",
        "ends_at",
        "image_url",
        "organizer",
    ]


def test_primary_change_updates_the_row_without_blanking_fills(session: Session) -> None:
    first = upsert_event(session, normalize(uticket_record(), now=NOW), now=NOW)
    upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    renamed = normalize(uticket_record(title="Eclipse Festival 2026"), now=NOW)
    third = upsert_event(session, renamed, now=NOW)
    assert third.event_id == first.event_id and third.version_written
    assert third.status is EventStatus.UPDATED and not third.merged
    event = session.get_one(Event, first.event_id)
    assert event.title == "Eclipse Festival 2026" and event.version == 3
    assert event.description is not None and event.image_url is not None
    assert event.organizer is not None and event.category is not None
    assert event.category.slug == "party"


def test_secondary_change_fills_later_and_never_overrides(session: Session) -> None:
    first = upsert_event(session, normalize(sympla_record(description=None), now=NOW), now=NOW)
    upsert_event(session, normalize(uticket_record(), now=NOW), now=NOW)
    event = session.get_one(Event, first.event_id)
    assert event.description is None

    described = normalize(
        uticket_record(title="Eclipse Festival (uTicket)", description="Do uTicket."), now=NOW
    )
    third = upsert_event(session, described, now=NOW)
    assert third.event_id == first.event_id and third.version_written and not third.merged
    event = session.get_one(Event, first.event_id)
    assert event.title == "Eclipse Festival"  # the primary's title stands
    assert event.description == "Do uTicket."
    assert event.version == 3
    version = session.scalars(
        select(EventVersion).where(EventVersion.event_id == event.id, EventVersion.version == 3)
    ).one()
    assert version.content["reason"] == "listing filled"
    assert version.content["filled"] == {"description": "Do uTicket."}


def test_ambiguous_pair_stays_separate_and_flags(session: Session) -> None:
    # two Sympla listings an hour apart at the same place: the platform's own duplicate
    a = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    b = upsert_event(
        session,
        normalize(
            sympla_record(
                external_id="3600002",
                starts_at=datetime(2026, 10, 31, 19, 0, tzinfo=UTC),
            ),
            now=NOW,
        ),
        now=NOW,
    )
    assert b.created and b.review and not b.merged and b.event_id != a.event_id
    # a third source now has two candidates: separate, flagged, not merged
    c = upsert_event(session, normalize(uticket_record(), now=NOW), now=NOW)
    assert c.created and c.review and not c.merged
    assert counts(session) == {"events": 3, "listings": 3, "versions": 3, "review": 3}

    # and a second run of everything changes nothing
    for rec in (
        sympla_record(),
        sympla_record(external_id="3600002", starts_at=datetime(2026, 10, 31, 19, 0, tzinfo=UTC)),
        uticket_record(),
    ):
        again = upsert_event(session, normalize(rec, now=NOW), now=NOW)
        assert not again.created and not again.merged and not again.version_written
        assert again.review  # still under review: the look-alikes are still there
    assert counts(session) == {"events": 3, "listings": 3, "versions": 3, "review": 3}


def test_merge_revives_an_event_removed_from_its_first_source(session: Session) -> None:
    first = upsert_event(session, normalize(sympla_record(), now=NOW), now=NOW)
    assert mark_missing(session, "sympla", set(), now=NOW) == 1
    second = upsert_event(session, normalize(uticket_record(), now=NOW), now=NOW)
    assert second.merged and second.event_id == first.event_id
    assert second.status is EventStatus.SCHEDULED
    event = session.get_one(Event, first.event_id)
    assert event.status is EventStatus.SCHEDULED and event.version == 3


def test_online_events_merge_on_title_and_time(session: Session) -> None:
    online = {"venue_name": None, "address": None, "city": None, "state": None}
    online |= {"latitude": None, "longitude": None}
    first = upsert_event(session, normalize(sympla_record(**online), now=NOW), now=NOW)
    second = upsert_event(session, normalize(uticket_record(**online), now=NOW), now=NOW)
    assert second.merged and second.event_id == first.event_id
    event = session.get_one(Event, first.event_id)
    assert event.is_online and event.venue is None
