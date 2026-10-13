"""Offline parsing tests for the Bilheteria Digital adapter against sanitized fixtures."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evcrawl.cities import City, for_source, resolve
from evcrawl.pipeline.fetch import FetchError
from evcrawl.pipeline.normalize import normalize
from evcrawl.sources.base import Discovered
from evcrawl.sources.bilheteriadigital import (
    EVENT_URL,
    LISTING_URL,
    BilheteriaDigitalAdapter,
    calendar_sessions,
    lot_name,
    parse_listing,
    record_from_page,
    split_city,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "bilheteriadigital"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

CARIACICA = "hot-wheels-monster-trucks-2027-cariacica-31-de-julho"
CALENDAR = "black-mirror"
PASS = "passaporte-festival-santo-pagode-30-de-outubro"
THEATRE = "dom-quixote-uma-aventura-sem-palavras-14-de-outubro-sessao-10h00"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def event(slug: str) -> str:
    return fixture(f"event-{slug}.html")


def url(slug: str) -> str:
    return EVENT_URL.format(slug=slug)


class FakeFetch:
    """Serves fixtures by URL and records every request; raises FetchError(404) for the rest."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, source: str, url: str) -> str:
        assert source == "bilheteriadigital"
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


# --- fixtures hygiene (AGENTS.md §10)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_at_sign_but_the_redacted_address(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert set(EMAIL.findall(text)) <= {"redacted@example.org"}
    assert text.count("@") == text.count("redacted@example.org")


# --- helpers


def test_split_city() -> None:
    assert split_city("Cariacica - ES") == ("Cariacica", "ES")
    assert split_city("  São José dos Campos -  SP ") == ("São José dos Campos", "SP")
    assert split_city("{{cidade}} - {{estado}}") == ("", "")
    assert split_city("") == ("", "")


def test_parse_listing_reads_every_card() -> None:
    items = parse_listing(fixture("listing-es.html"))
    assert [i["slug"] for i in items] == [
        CARIACICA,
        "hot-wheels-monster-trucks-live-2027-cariacica-sessao-18h30-31-de-julho",
    ]
    assert items[0] == {
        "slug": CARIACICA,
        "title": "HOT WHEELS MONSTER TRUCKS LIVE - 2027 | CARIACICA - SESSÃO 11H30",
        "date": "31 de Julho",
        "city": "Cariacica",
        "state": "ES",
        "venue": "ESTÁDIO KLEBER ANDRADE",
        "image": "https://s3.amazonaws.com/assets.bilheteriadigital/eventos/70279a.jpg?1786092317000",
    }
    sp = parse_listing(fixture("listing-sp.html"))
    assert len(sp) == 21  # the whole state on one page, 2026-10-13
    assert {i["state"] for i in sp} == {"SP"}
    assert sp[0]["slug"] == CALENDAR and sp[0]["date"] == ""  # a running exhibition


def test_parse_listing_skips_the_template_and_foreign_links() -> None:
    body = (
        '<ul><li class="box-li-evento"><a href="{{link}}"><div class="cidade-box-evento">'
        "{{cidade}} - {{estado}}</div></a></li>"
        '<li class="box-li-evento"><a href="https://example.org/x"></a></li>'
        '<li class="box-li-evento"><a href="/a/b"></a></li></ul>'
    )
    assert parse_listing(body) == []
    assert parse_listing("<div class='lista-eventos'></div>") == []


def test_lot_name_drops_a_calendar_session() -> None:
    assert lot_name("13/10-  INGRESSO MEIA ENTRADA | SESSÃO 14h00") == "INGRESSO MEIA ENTRADA"
    assert lot_name("13/10- INGRESSO COMBO | SESSÃO 9h") == "INGRESSO COMBO"
    assert lot_name("Plateia (Inteira)") == "Plateia (Inteira)"


def test_registry_keys_are_the_city_names() -> None:
    assert resolve("cariacica").key("bilheteriadigital") == "Cariacica"
    assert resolve("sao-paulo").key("bilheteriadigital") == "São Paulo"
    assert len(for_source("bilheteriadigital")) == 32


# --- discover()


async def discovered(adapter: BilheteriaDigitalAdapter, city: City | None) -> list[Discovered]:
    items = [d async for d in adapter.discover(city)]
    assert all(isinstance(d, Discovered) for d in items)
    return [d for d in items if isinstance(d, Discovered)]


async def test_discover_filters_the_state_listing_by_city() -> None:
    fake = FakeFetch({LISTING_URL.format(state="SP"): "listing-sp.html"})
    adapter = BilheteriaDigitalAdapter(fake)
    items = await discovered(adapter, resolve("sao-paulo"))
    expected = [
        i["slug"] for i in parse_listing(fixture("listing-sp.html")) if i["city"] == "São Paulo"
    ]
    assert [d.external_id for d in items] == expected and len(expected) == 6
    assert items[0].url == url(CALENDAR)
    # São Bernardo do Campo and the other SP towns are not registry cities
    assert all("sao-bernardo" not in d.url for d in items)
    assert await discovered(adapter, resolve("vitoria")) == []  # ES: 404 here
    assert fake.calls == [LISTING_URL.format(state="SP"), LISTING_URL.format(state="ES")]


async def test_discover_reads_each_state_once_per_instance() -> None:
    fake = FakeFetch({LISTING_URL.format(state="ES"): "listing-es.html"})
    adapter = BilheteriaDigitalAdapter(fake)
    assert len(await discovered(adapter, resolve("cariacica"))) == 2
    assert await discovered(adapter, resolve("vitoria")) == []
    assert fake.calls == [LISTING_URL.format(state="ES")]


async def test_discover_all_cities_yields_each_event_once() -> None:
    fake = FakeFetch(
        {
            LISTING_URL.format(state="ES"): "listing-es.html",
            LISTING_URL.format(state="SP"): "listing-sp.html",
            LISTING_URL.format(state="AC"): "listing-ac.html",
        }
    )
    items = await discovered(BilheteriaDigitalAdapter(fake), None)
    ids = [d.external_id for d in items]
    assert len(ids) == len(set(ids)) == 2 + 6 + 7
    # one listing request per UF of the registry, the 404s logged and skipped
    assert len(fake.calls) == len({c.uf for c in for_source("bilheteriadigital")})


async def test_discover_logs_and_skips_an_unavailable_listing() -> None:
    async def broken(source: str, url: str) -> str:
        raise FetchError(url, status=503)

    assert await discovered(BilheteriaDigitalAdapter(broken), resolve("vitoria")) == []


async def test_discovered_ids_match_the_fetched_records() -> None:
    """ADR 0013: the crawl finds the stored listing by the id discover() carries."""
    fake = FakeFetch(
        {
            LISTING_URL.format(state="ES"): "listing-es.html",
            url(CARIACICA): f"event-{CARIACICA}.html",
        }
    )
    adapter = BilheteriaDigitalAdapter(fake)
    item = (await discovered(adapter, resolve("cariacica")))[0]
    assert item.url == url(CARIACICA)
    assert (await adapter.fetch(item.url)).external_id == item.external_id == CARIACICA


async def test_discover_fingerprint_follows_the_card() -> None:
    body = fixture("listing-es.html")

    async def fingerprint_of(text: str) -> str:
        async def fake(source: str, url: str) -> str:
            return text

        items = await discovered(BilheteriaDigitalAdapter(fake), resolve("cariacica"))
        return items[0].fingerprint

    original = await fingerprint_of(body)
    assert await fingerprint_of(body.replace("\n", "\n  ")) == original  # whitespace only
    assert await fingerprint_of(body.replace("SESSÃO 11H30", "SESSÃO 12H00", 1)) != original


# --- fetch()


async def test_fetch_parses_an_event_page() -> None:
    fake = FakeFetch({url(CARIACICA): f"event-{CARIACICA}.html"})
    rec = await BilheteriaDigitalAdapter(fake).fetch(url(CARIACICA))
    assert fake.calls == [url(CARIACICA)]

    assert rec.source == "bilheteriadigital"
    assert rec.external_id == CARIACICA
    assert str(rec.source_url) == url(CARIACICA)
    assert rec.title == "HOT WHEELS MONSTER TRUCKS LIVE - 2027 | CARIACICA - SESSÃO 11H30"
    assert rec.starts_at == datetime(2027, 7, 31, 14, 30, tzinfo=UTC)  # 11:30 in Cariacica
    assert rec.ends_at is None
    assert rec.timezone == "America/Sao_Paulo"
    assert rec.venue_name == "ESTÁDIO KLEBER ANDRADE"
    assert rec.address == "R. Rio Branco - Rio Branco, Cariacica - ES, 29147-709"
    assert rec.city == "Cariacica" and rec.state == "ES"
    assert (rec.latitude, rec.longitude) == (-20.33282389367, -40.38524162778)
    assert rec.category is None  # the site has no category
    assert rec.organizer is None
    assert rec.description is not None
    assert rec.description.startswith("HOT WHEELS MONSTER TRUCKS LIVE")
    assert "<br" not in rec.description and "\n" in rec.description
    assert str(rec.image_url).startswith(
        "https://s3.amazonaws.com/assets.bilheteriadigital/eventos/70279e.jpg"
    )
    assert rec.raw["evento_id"] == "70279"
    # price + fee, one lot per ticket row
    assert len(rec.price_lots) == 27
    inteira = next(p for p in rec.price_lots if p.name == "CADEIRAS MONSTER TRUCKS A/B | INTEIRA")
    assert inteira.amount == Decimal("446.40")  # 372 + 74.40
    assert min(p.amount for p in rec.price_lots) == Decimal("191.52")


async def test_fetch_parses_a_zero_fee_event() -> None:
    rec = record_from_page(url(THEATRE), event(THEATRE))
    assert [(p.name, p.amount) for p in rec.price_lots][:3] == [
        ("Plateia (Meia)", Decimal("15.00")),
        ("Plateia (Meia Cliente CAIXA)", Decimal("15.00")),
        ("Plateia (Inteira)", Decimal("30.00")),
    ]
    assert rec.city == "Rio de Janeiro" and rec.state == "RJ"
    assert rec.title == "Dom Quixote – Uma Aventura Sem Palavras"


def test_calendar_event_spans_every_session() -> None:
    sessions = calendar_sessions(event(CALENDAR))
    assert len(sessions) == 215
    assert sessions[0] == datetime(2026, 10, 8, 14, 0)
    assert sessions[-1] == datetime(2026, 10, 31, 21, 0)
    rec = record_from_page(url(CALENDAR), event(CALENDAR))
    assert rec.starts_at == datetime(2026, 7, 31, 17, 0, tzinfo=UTC)  # the opening
    assert rec.ends_at == datetime(2026, 11, 1, 0, 0, tzinfo=UTC)  # 21:00 on 2026-10-31
    assert rec.city == "São Paulo" and rec.venue_name == "Shopping Eldorado"
    # each ticket repeats for every session: one lot per ticket
    assert [(p.name, p.amount) for p in rec.price_lots] == [
        ("INGRESSO MEIA ENTRADA", Decimal("67.28")),
        ("INGRESSO PROMO SOCIAL", Decimal("73.49")),
        ("INGRESSO INTEIRA", Decimal("134.55")),
        ("INGRESSO COMBO", Decimal("62.10")),
    ]
    assert calendar_sessions("<html></html>") == []
    assert calendar_sessions("<script>window.BD_CALENDARIO = {broken</script>") == []


def test_date_range_and_the_local_zone() -> None:
    """Rio Branco: the site shows 19:00 (Brasília time) for a 19:00 start in Acre."""
    rec = record_from_page(url(PASS), event(PASS))
    assert rec.timezone == "America/Rio_Branco"
    assert rec.starts_at == datetime(2026, 10, 31, 0, 0, tzinfo=UTC)  # 19:00 UTC-5
    assert rec.ends_at == datetime(2026, 11, 2, 0, 0, tzinfo=UTC)  # 01 Nov, same wall time
    assert rec.price_lots[0].amount == Decimal("66.00")
    assert rec.address is None  # no "Endereço:" line on this page


def test_record_tolerates_missing_coordinates_and_rejects_non_event_pages() -> None:
    body = event(THEATRE).replace("value='-22.9088732,-43.181033'", "value=''")
    rec = record_from_page(url(THEATRE), body)
    assert rec.latitude is None and rec.longitude is None
    with pytest.raises(ValueError, match="not an event page"):
        record_from_page(url("x"), "<html><body>404</body></html>")


def test_normalize_keeps_the_record() -> None:
    rec = record_from_page(url(CARIACICA), event(CARIACICA))
    n = normalize(rec, now=datetime(2026, 10, 13, tzinfo=UTC))
    assert n.price_lots_pending is False
    assert n.category == "uncategorized"


async def test_fetch_rejects_foreign_urls() -> None:
    adapter = BilheteriaDigitalAdapter(FakeFetch({}))
    for bad in ("https://www.sympla.com.br/evento/x/1", "https://www.bilheteriadigital.com/a/b"):
        with pytest.raises(ValueError, match="not a Bilheteria Digital event URL"):
            await adapter.fetch(bad)
