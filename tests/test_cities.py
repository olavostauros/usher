"""The city registry: 32 rows, zones, resolution, per-source lists (Phase 3 spec 1, §3)."""

from zoneinfo import ZoneInfo

import pytest

from evcrawl import cities
from evcrawl.cities import (
    CAPITALS,
    CITIES,
    DEFAULT_TIMEZONE,
    GREATER_VITORIA,
    UF_TIMEZONE,
    City,
    by_slug,
    fold,
    for_source,
    resolve,
    slugs,
    timezone_for_uf,
)
from evcrawl.sources import REGISTRY

SOURCES = sorted(REGISTRY)


def test_registry_has_the_32_cities_greater_vitoria_first() -> None:
    assert len(CITIES) == 32 and CITIES == GREATER_VITORIA + CAPITALS
    assert [c.slug for c in GREATER_VITORIA] == [
        "vitoria",
        "vila-velha",
        "serra",
        "cariacica",
        "viana",
        "guarapari",
    ]
    assert all(c.uf == "ES" and c.enabled for c in GREATER_VITORIA)
    assert len(CAPITALS) == 26 and all(c.enabled for c in CAPITALS)  # since task 8
    assert [c.uf for c in CAPITALS] == sorted(c.uf for c in CAPITALS)  # by UF
    assert sorted([c.uf for c in CAPITALS] + ["ES"]) == sorted(UF_TIMEZONE)  # every UF once


def test_slugs_and_ibge_codes_are_unique_and_well_formed() -> None:
    assert len({c.slug for c in CITIES}) == 32
    assert len({c.ibge_code for c in CITIES}) == 32
    for city in CITIES:
        assert city.slug == fold(city.name).replace(" ", "-") and "_" not in city.slug
        assert 1_000_000 <= city.ibge_code <= 9_999_999  # IBGE 7-digit municipality code
        assert city.ibge_code // 100_000 == {  # the first two digits are the state
            "AC": 12, "AL": 27, "AM": 13, "AP": 16, "BA": 29, "CE": 23, "DF": 53, "ES": 32,
            "GO": 52, "MA": 21, "MG": 31, "MS": 50, "MT": 51, "PA": 15, "PB": 25, "PE": 26,
            "PI": 22, "PR": 41, "RJ": 33, "RN": 24, "RO": 11, "RR": 14, "RS": 43, "SC": 42,
            "SE": 28, "SP": 35, "TO": 17,
        }[city.uf]  # fmt: skip
    assert slugs() == [c.slug for c in CITIES]


def test_every_city_has_a_key_for_every_source() -> None:
    for city in CITIES:
        assert set(city.keys) == set(SOURCES)
        assert city.key("sympla") == f"{city.slug}-{city.uf.lower()}"
        for source in ("uticket", "blueticket", "ticketbolt", "zig"):
            assert city.key(source) == city.name
        assert city.key("nope") is None


def test_zones_come_from_the_uf_table_and_load() -> None:
    for city in CITIES:
        assert city.timezone == UF_TIMEZONE[city.uf]
        ZoneInfo(city.timezone)  # known to the image's tzdata
    assert by_slug("manaus").timezone == "America/Manaus"
    assert by_slug("sao-paulo").timezone == DEFAULT_TIMEZONE
    assert by_slug("macapa").timezone == "America/Belem"
    assert by_slug("palmas").timezone == "America/Araguaina"


def test_timezone_for_uf() -> None:
    assert timezone_for_uf("AM") == "America/Manaus"
    assert timezone_for_uf(" am ") == "America/Manaus"
    assert timezone_for_uf("ES") == DEFAULT_TIMEZONE
    assert timezone_for_uf("XX") == DEFAULT_TIMEZONE
    assert timezone_for_uf(None) == DEFAULT_TIMEZONE
    assert timezone_for_uf("") == DEFAULT_TIMEZONE


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("vitoria", "vitoria"),
        ("vitoria-es", "vitoria"),  # the Phase 1/2 ``--city`` form
        ("Vitória", "vitoria"),
        ("VITORIA", "vitoria"),
        ("Vila Velha", "vila-velha"),
        ("vila_velha", "vila-velha"),
        ("sao-paulo", "sao-paulo"),
        ("São Paulo", "sao-paulo"),
        ("sao paulo", "sao-paulo"),
        ("sao-paulo-sp", "sao-paulo"),
        ("Rio de Janeiro", "rio-de-janeiro"),
        ("  manaus ", "manaus"),
    ],
)
def test_resolve_accepts_slug_old_form_and_name(text: str, slug: str) -> None:
    assert resolve(text) is by_slug(slug)


def test_resolve_and_by_slug_reject_unknown_cities() -> None:
    with pytest.raises(ValueError, match="unknown city 'linhares'; known: vitoria, vila-velha"):
        resolve("linhares")
    with pytest.raises(ValueError, match="unknown city"):
        by_slug("Vitória")  # by_slug is exact


@pytest.mark.parametrize("source", SOURCES)
def test_for_source_lists_every_enabled_city_in_registry_order(source: str) -> None:
    assert for_source(source) == list(CITIES)  # every city is enabled since task 8
    assert for_source(source, enabled_only=False) == list(CITIES)
    assert for_source("nope") == []


def test_for_source_skips_a_paused_city_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``enabled = False`` pauses a city: it resolves, but no standing job lists it."""
    paused = City("manaus", "Manaus", "AM", 1302603, "America/Manaus", False, {"zig": "Manaus"})
    monkeypatch.setattr(cities, "CITIES", GREATER_VITORIA + (paused,))
    assert for_source("zig") == list(GREATER_VITORIA)
    assert for_source("zig", enabled_only=False) == [*GREATER_VITORIA, paused]
    assert resolve("manaus").slug == "manaus"  # the alias table is built at import


def test_city_is_frozen_and_compared_by_identity_fields() -> None:
    city = by_slug("vitoria")
    with pytest.raises(AttributeError):
        city.enabled = False  # type: ignore[misc]
    assert city == City("vitoria", "Vitória", "ES", 3205309, DEFAULT_TIMEZONE, keys={})
    assert hash(city) == hash(City("vitoria", "Vitória", "ES", 3205309, DEFAULT_TIMEZONE))
