"""Table-driven tests for EventRecord -> NormalizedEvent."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from evcrawl.db.models import EventStatus
from evcrawl.pipeline.normalize import (
    CATEGORIES,
    SOURCE_CATEGORIES,
    UNCATEGORIZED,
    collapse_whitespace,
    map_category,
    normalize,
    normalize_key,
    postal_code,
    to_utc,
    unaccent,
)
from evcrawl.sources.base import EventRecord, PriceLot

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def record(**overrides: Any) -> EventRecord:
    base: dict[str, Any] = {
        "source": "sympla",
        "external_id": "1",
        "source_url": "https://www.sympla.com.br/evento/x/1",
        "title": "  Forró   da  Praça ",
        "starts_at": datetime(2026, 10, 1, 20, 0, tzinfo=UTC),
        "ends_at": datetime(2026, 10, 2, 1, 0, tzinfo=UTC),
        "venue_name": "Praça do Papa",
        "address": "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES, 29050-662",
        "city": "Vitória",
        "state": "es",
        "latitude": -20.31,
        "longitude": -40.30,
        "organizer": "  Coletivo  Forró ",
        "category": "musica",
        "description": "  Primeira linha.\n\nSegunda linha.  ",
        "image_url": "https://images.sympla.com.br/x.png",
        "price_lots": [PriceLot(name="Pista", amount=Decimal("23.99"))],
    }
    base.update(overrides)
    return EventRecord(**base)


# --- text helpers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Vitória", "Vitoria"),
        ("São João, Açaí & Coração", "Sao Joao, Acai & Coracao"),
        ("ÀÉÎÕÜ ç", "AEIOU c"),
        ("plain", "plain"),
    ],
)
def test_unaccent(text: str, expected: str) -> None:
    assert unaccent(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  Forró   da  Praça ", "forro da praca"),
        ("O Mistério da Casa Amarela – 22/09/2026", "o misterio da casa amarela 22 09 2026"),
        ("SLIPMAMI em Vila Velha/ES!!!", "slipmami em vila velha es"),
        ("", ""),
    ],
)
def test_normalize_key(text: str, expected: str) -> None:
    assert normalize_key(text) == expected


def test_collapse_whitespace() -> None:
    assert collapse_whitespace("  a \n\t b   c ") == "a b c"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Av. X, 1, Centro, Vitória - ES, 29050-662", "29050-662"),
        ("Rua Y 10 29102920", "29102-920"),
        ("no zip here", None),
        (None, None),
    ],
)
def test_postal_code(address: str | None, expected: str | None) -> None:
    assert postal_code(address) == expected


def test_to_utc_reads_naive_as_local_wall_time() -> None:
    naive = datetime(2026, 9, 26, 8, 0)
    assert to_utc(naive, "America/Sao_Paulo") == datetime(2026, 9, 26, 11, 0, tzinfo=UTC)
    aware = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
    assert to_utc(aware, "America/Sao_Paulo") == aware


# --- categories


@pytest.mark.parametrize(
    ("source", "slug", "expected"),
    [
        ("sympla", "musica", "music"),
        ("sympla", "Teatro-Stand-Up-E-Danca", "theatre-and-dance"),
        ("sympla", "informatica-tecnologia-e-programacao", "tech"),
        ("sympla", "outro", UNCATEGORIZED),
        ("sympla", "categoria-inexistente", UNCATEGORIZED),
        ("sympla", None, UNCATEGORIZED),
        ("nope", "musica", UNCATEGORIZED),
    ],
)
def test_map_category(source: str, slug: str | None, expected: str) -> None:
    assert map_category(source, slug) == expected


def test_every_source_mapping_targets_the_internal_taxonomy() -> None:
    for source, table in SOURCE_CATEGORIES.items():
        for slug, internal in table.items():
            assert internal in CATEGORIES, f"{source}:{slug} -> {internal}"


# --- normalize()


def test_normalize_full_record() -> None:
    n = normalize(record(), now=NOW)
    assert n.title == "Forró da Praça"
    assert n.title_normalized == "forro da praca"
    assert n.description == "Primeira linha.\n\nSegunda linha."
    assert n.starts_at == datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    assert n.status is EventStatus.SCHEDULED
    assert n.category == "music" and n.category_source == "musica"
    assert n.venue is not None
    assert n.venue.name == "Praça do Papa"
    assert (
        n.venue.address_normalized == "avenida cezar hilal 700 bento ferreira vitoria es 29050 662"
    )
    assert n.venue.postal_code == "29050-662"
    assert n.venue.state == "ES"
    assert (n.venue.latitude, n.venue.longitude) == (-20.31, -40.30)
    assert n.organizer is not None
    assert (n.organizer.name, n.organizer.name_normalized) == ("Coletivo Forró", "coletivo forro")
    assert n.price_lots == [PriceLot(name="Pista", amount=Decimal("23.99"))]
    assert n.price_lots_pending is False
    assert n.is_online is False
    assert len(n.content_hash) == 64


@pytest.mark.parametrize(
    ("lots", "raw", "expected_lots", "pending"),
    [
        ([PriceLot(name="Gratuito", amount=Decimal(0))], {}, [("Gratuito", "0.00")], False),
        (
            [
                PriceLot(name="Pista", amount=Decimal("23.99")),
                PriceLot(name="VIP", amount=Decimal("60.5")),
            ],
            {},
            [("Pista", "23.99"), ("VIP", "60.50")],
            False,
        ),
        (
            [
                PriceLot(name=" Pista ", amount=Decimal("23.99")),
                PriceLot(name="Pista", amount=Decimal("23.99")),
            ],
            {},
            [("Pista", "23.99")],
            False,
        ),
        ([], {"price_lots_pending": True}, [], True),
        ([], {}, [], True),
        (
            [PriceLot(name="Lote 1", amount=Decimal("10"))],
            {"price_lots_pending": True},
            [("Lote 1", "10.00")],
            True,
        ),
    ],
)
def test_price_rule(
    lots: list[PriceLot],
    raw: dict[str, object],
    expected_lots: list[tuple[str, str]],
    pending: bool,
) -> None:
    n = normalize(record(price_lots=lots, raw=raw), now=NOW)
    assert [(lot.name, str(lot.amount)) for lot in n.price_lots] == expected_lots
    assert n.price_lots_pending is pending


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, EventStatus.SCHEDULED),
        ({"cancelled": True}, EventStatus.CANCELLED),
        ({"cancelled": True, "closed": True}, EventStatus.CANCELLED),
        ({"closed": True}, EventStatus.ENDED),
        ({"ends_at": datetime(2026, 9, 20, 11, 0, tzinfo=UTC)}, EventStatus.ENDED),
        (
            {"starts_at": datetime(2026, 9, 20, 9, 0, tzinfo=UTC), "ends_at": None},
            EventStatus.ENDED,
        ),
        (
            {
                "starts_at": datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
            },
            EventStatus.SCHEDULED,
        ),
    ],
)
def test_status_mapping(overrides: dict[str, Any], expected: EventStatus) -> None:
    assert normalize(record(**overrides), now=NOW).status is expected


def test_naive_times_are_read_in_the_record_timezone() -> None:
    n = normalize(record(starts_at=datetime(2026, 9, 26, 8, 0), ends_at=None), now=NOW)
    assert n.starts_at == datetime(2026, 9, 26, 11, 0, tzinfo=UTC)
    assert n.timezone == "America/Sao_Paulo"


def test_venue_key_falls_back_to_name_and_city_without_address() -> None:
    n = normalize(record(address=None), now=NOW)
    assert n.venue is not None
    assert n.venue.address is None
    assert n.venue.address_normalized == "praca do papa vitoria es"
    assert n.venue.postal_code is None


def test_online_event_has_no_venue() -> None:
    n = normalize(
        record(venue_name=None, address=None, city=None, state=None, raw={"online": True}), now=NOW
    )
    assert n.venue is None
    assert n.is_online is True


def test_missing_organizer_and_category() -> None:
    n = normalize(record(organizer="  ", category=None), now=NOW)
    assert n.organizer is None
    assert n.category == UNCATEGORIZED and n.category_source is None


def test_content_hash_is_stable_and_ignores_raw() -> None:
    a = normalize(record(raw={"event": {"x": 1}}), now=NOW)
    b = normalize(record(raw={"event": {"x": 2}}), now=NOW)
    assert a.content_hash == b.content_hash
    c = normalize(record(title="Forró da Praça (nova data)"), now=NOW)
    assert c.content_hash != a.content_hash
    d = normalize(record(cancelled=True), now=NOW)
    assert d.content_hash != a.content_hash


def test_normalize_real_sympla_event_fixture() -> None:
    from pathlib import Path

    from evcrawl.sources.sympla import _record_from_event_page, parse_tickets

    fixtures = Path(__file__).parent / "fixtures" / "http" / "sympla"
    html = (fixtures / "event-3537075.html").read_text(encoding="utf-8")
    lots = parse_tickets((fixtures / "tickets-3537075.json").read_text(encoding="utf-8"))
    n = normalize(
        _record_from_event_page(
            "https://www.sympla.com.br/evento/pmi-ai-connect/3537075", html, lots
        ),
        now=NOW,
    )
    assert n.title_normalized == "pmi ai connect"
    assert n.status is EventStatus.SCHEDULED
    assert n.category == UNCATEGORIZED and n.category_source == "outro"
    assert n.venue is not None and n.venue.postal_code == "29050-662"
    assert (
        n.venue.address_normalized == "avenida cezar hilal 700 bento ferreira vitoria es 29050 662"
    )
    assert [str(lot.amount) for lot in n.price_lots] == ["140.40", "108.00"]
    assert n.price_lots_pending is False
