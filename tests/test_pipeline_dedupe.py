"""Dedupe scaffold: match_key offline; candidate SQL on the scratch db."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from evcrawl.db.models import Event
from evcrawl.pipeline.dedupe import find_candidates, flag_duplicates, match_key
from evcrawl.pipeline.normalize import normalize
from evcrawl.pipeline.upsert import upsert_event
from evcrawl.sources.base import EventRecord

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


def test_flag_duplicates_marks_both_sides_and_merges_nothing(session: Session) -> None:
    first = upsert_event(session, normalize(record("1"), now=NOW), now=NOW)
    assert flag_duplicates(session, normalize(record("1"), now=NOW), first.event_id) == []

    near = normalize(
        record("2", starts_at=START + timedelta(hours=1), latitude=NEAR[0], longitude=NEAR[1]),
        now=NOW,
    )
    second = upsert_event(session, near, now=NOW)
    assert flag_duplicates(session, near, second.event_id) == [first.event_id]

    flags = dict(session.execute(select(Event.id, Event.dedupe_review)).all())
    assert flags == {first.event_id: True, second.event_id: True}
    assert session.scalar(select(Event.id).where(Event.id == second.event_id)) is not None
