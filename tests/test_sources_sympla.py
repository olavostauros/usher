"""Offline parsing tests for the Sympla adapter against sanitized fixtures."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evcrawl.cities import for_source, resolve
from evcrawl.pipeline.fetch import FetchError
from evcrawl.sources.base import PriceLot
from evcrawl.sources.sympla import (
    LISTING_URL,
    TICKETS_URL,
    SymplaAdapter,
    _flight_payload,
    _next_data,
    parse_listing,
    parse_listing_page,
    parse_tickets,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "sympla"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

EVENT_URL = "https://www.sympla.com.br/evento/pmi-ai-connect/3537075"
BILETO_URL = "https://bileto.sympla.com.br/event/115574"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetch:
    """Serves fixtures by URL and records every request; raises FetchError for the rest."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, source: str, url: str) -> str:
        assert source == "sympla"
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


def vitoria_routes() -> dict[str, str]:
    routes = {LISTING_URL.format(slug="vitoria-es", page=1): "listing-vitoria-p1.html"}
    for page in range(2, 9):
        routes[LISTING_URL.format(slug="vitoria-es", page=page)] = "listing-vitoria-p1.html"
    routes[LISTING_URL.format(slug="vitoria-es", page=9)] = "listing-vitoria-p9-empty.html"
    return routes


def sao_paulo_routes() -> dict[str, str]:
    """The hub page walk, cut short: page 1, then the empty page."""
    return {
        LISTING_URL.format(slug="sao-paulo-sp", page=1): "listing-sao-paulo-p1.html",
        LISTING_URL.format(slug="sao-paulo-sp", page=2): "listing-sao-paulo-p300-empty.html",
    }


# --- fixtures hygiene (AGENTS.md §10, spec task 1 done-criterion)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_email_but_the_redacted_one(path: Path) -> None:
    found = set(EMAIL.findall(path.read_text(encoding="utf-8")))
    assert found <= {"redacted@example.org"}


# --- helpers


def test_flight_payload_concatenates_decoded_chunks() -> None:
    html = (
        '<script>self.__next_f.push([1,"a:\\"x\\"\\n"])</script>'
        '<script>self.__next_f.push([1,"b:{\\"k\\":1}"])</script>'
    )
    assert _flight_payload(html) == 'a:"x"\nb:{"k":1}'


def test_flight_payload_of_real_listing_has_search_response() -> None:
    payload = _flight_payload(fixture("listing-vitoria-p1.html"))
    assert '"searchDataResult":{"data":[' in payload
    assert '"total":189,"limit":24,"page":1' in payload


def test_next_data_parses_the_script() -> None:
    html = '<html><script id="__NEXT_DATA__" type="application/json">{"props":{"a":1}}</script>'
    assert _next_data(html) == {"props": {"a": 1}}


def test_next_data_requires_the_script() -> None:
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        _next_data("<html></html>")


# --- listing pages


def test_parse_listing_page_1() -> None:
    events = parse_listing(fixture("listing-vitoria-p1.html"))
    assert len(events) == 24
    by_id = {e["id"]: e for e in events}
    ev = by_id[3549048]
    assert ev["name"] == "II Encontro Estadual Mulheres na TI ES"
    assert (
        ev["url"]
        == "https://www.sympla.com.br/evento/ii-encontro-estadual-mulheres-na-ti-es/3549048"
    )
    assert ev["start_date"] == "2026-09-24T22:00:00+00:00"
    assert ev["location"]["city"] == "Vila Velha"
    assert ev["location"]["lat"] == pytest.approx(-20.3335011)
    urls = {e["url"] for e in events}
    assert BILETO_URL in urls
    assert EVENT_URL in urls


def test_parse_listing_last_page_is_empty() -> None:
    assert parse_listing(fixture("listing-vitoria-p9-empty.html")) == []


def test_parse_listing_page_hub_template() -> None:
    # The 11 biggest capitals get a curated hub page: ``dataSectionMoreEvents``,
    # 16 per page, no ``searchDataResult`` (ADR 0003 addendum).
    template, events = parse_listing_page(fixture("listing-sao-paulo-p1.html"))
    assert template == "hub" and len(events) == 16
    assert len({e["id"] for e in events}) == 16
    assert {e["location"]["city"] for e in events} == {"São Paulo"}
    assert {e["location"]["state"] for e in events} == {"SP"}
    assert all(e["url"].startswith("https://") for e in events)
    assert parse_listing_page(fixture("listing-vitoria-p1.html"))[0] == "city"


def test_parse_listing_hub_last_page_is_empty() -> None:
    assert parse_listing_page(fixture("listing-sao-paulo-p300-empty.html")) == ("hub", [])


def test_parse_listing_rejects_page_without_search_data() -> None:
    with pytest.raises(ValueError, match="searchDataResult"):
        parse_listing('<script>self.__next_f.push([1,"0:[]"])</script>')


# --- discover()


async def test_discover_paginates_until_an_empty_page() -> None:
    fake = FakeFetch(vitoria_routes())
    urls = [u async for u in SymplaAdapter(fake).discover(resolve("vitoria"))]
    assert len(fake.calls) == 9
    assert fake.calls[0] == "https://www.sympla.com.br/eventos/vitoria-es?page=1"
    assert fake.calls[-1] == "https://www.sympla.com.br/eventos/vitoria-es?page=9"
    # pages 2-8 repeat page 1 in this fake, so every URL is yielded once
    assert len(urls) == 24 == len(set(urls))
    assert EVENT_URL in urls
    assert BILETO_URL in urls
    assert all(u.startswith("https://www.sympla.com.br/evento/") or "bileto" in u for u in urls)


async def test_discover_logs_and_skips_an_unavailable_city() -> None:
    fake = FakeFetch(vitoria_routes())  # every other city 404s
    urls = [u async for u in SymplaAdapter(fake).discover(None)]
    assert len(urls) == 24
    slugs = {u.split("/eventos/")[1].split("?")[0] for u in fake.calls}
    assert slugs == {
        "vitoria-es",
        "vila-velha-es",
        "serra-es",
        "cariacica-es",
        "viana-es",
        "guarapari-es",
    }


async def test_discover_walks_a_hub_listing_until_the_empty_page() -> None:
    fake = FakeFetch(sao_paulo_routes())
    urls = [u async for u in SymplaAdapter(fake).discover(resolve("sao-paulo"))]
    assert fake.calls == [
        "https://www.sympla.com.br/eventos/sao-paulo-sp?page=1",
        "https://www.sympla.com.br/eventos/sao-paulo-sp?page=2",
    ]
    assert len(urls) == 16 == len(set(urls))


def test_registry_keys_are_the_listing_slugs() -> None:
    assert resolve("vitoria").key("sympla") == "vitoria-es"
    assert resolve("Vila Velha").key("sympla") == "vila-velha-es"
    assert resolve("serra-es").key("sympla") == "serra-es"
    assert resolve("sao-paulo").key("sympla") == "sao-paulo-sp"
    assert len(for_source("sympla")) == 6  # the capitals are not enabled yet
    with pytest.raises(ValueError, match="unknown city"):
        resolve("linhares")


# --- fetch(): event page + tickets


def event_routes() -> dict[str, str]:
    return {
        EVENT_URL: "event-3537075.html",
        TICKETS_URL.format(id=3537075): "tickets-3537075.json",
    }


async def test_fetch_parses_event_page_and_tickets() -> None:
    fake = FakeFetch(event_routes())
    rec = await SymplaAdapter(fake).fetch(EVENT_URL)
    assert fake.calls == [EVENT_URL, TICKETS_URL.format(id=3537075)]

    assert rec.source == "sympla"
    assert rec.external_id == "3537075"
    assert str(rec.source_url) == EVENT_URL
    assert rec.title == "PMI AI Connect"
    assert rec.starts_at == datetime(2026, 9, 26, 11, 0, tzinfo=UTC)
    assert rec.ends_at == datetime(2026, 9, 26, 16, 0, tzinfo=UTC)
    assert rec.timezone == "America/Sao_Paulo"
    assert rec.venue_name == "[Unimed Vitória] Auditório Sede Administrativa"
    assert rec.address == "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES, 29050-662"
    assert rec.city == "Vitória"
    assert rec.state == "ES"
    assert rec.latitude == pytest.approx(-20.3119792)
    assert rec.longitude == pytest.approx(-40.3050745)
    assert rec.organizer == "PMI- ES"
    assert rec.category == "outro"
    assert rec.description is not None and rec.description.startswith("O PMI AI CONNECT é")
    assert str(rec.image_url) == "https://images.sympla.com.br/6a8eea0e59ee2.png"
    assert rec.cancelled is False
    assert rec.closed is False
    assert rec.price_lots == [
        PriceLot(name="Público Geral - Lote 1", amount=Decimal("140.40")),
        PriceLot(name="Filiado - Lote 1", amount=Decimal("108.00")),
    ]
    assert "price_lots_pending" not in rec.raw
    assert rec.raw["tickets_fetched"] is True


async def test_fetch_raw_strips_host_description() -> None:
    page_event = _next_data(fixture("event-3537075.html"))["props"]["pageProps"]["hydrationData"][
        "eventHydration"
    ]["event"]
    assert "description" in page_event["eventsHost"]  # present in the source page...

    rec = await SymplaAdapter(FakeFetch(event_routes())).fetch(EVENT_URL)
    event = rec.raw["event"]
    assert isinstance(event, dict)
    assert "detail" in event  # HTML description kept only in raw
    assert "description" not in event["eventsHost"]  # ...never stored
    assert "@" not in json.dumps(rec.raw, ensure_ascii=False)


async def test_fetch_marks_paid_event_pending_when_tickets_fail() -> None:
    fake = FakeFetch({EVENT_URL: "event-3537075.html"})
    rec = await SymplaAdapter(fake).fetch(EVENT_URL)
    assert rec.price_lots == []
    assert rec.raw["price_lots_pending"] is True
    assert rec.raw["tickets_fetched"] is False


async def test_fetch_free_event_without_lots_gets_gratuito() -> None:
    html = fixture("event-3537075.html").replace(
        '"paymentEventType":"paid"', '"paymentEventType":"free"'
    )
    assert '"paymentEventType":"free"' in html
    tickets = json.dumps({"tickets": [], "groups": []})

    async def fake(source: str, url: str) -> str:
        return html if url == EVENT_URL else tickets

    rec = await SymplaAdapter(fake).fetch(EVENT_URL)
    assert rec.price_lots == [PriceLot(name="Gratuito", amount=Decimal(0))]
    assert "price_lots_pending" not in rec.raw


async def test_fetch_rejects_foreign_urls() -> None:
    with pytest.raises(ValueError, match="not a Sympla event URL"):
        await SymplaAdapter(FakeFetch({})).fetch("https://example.org/evento/x/1")


# --- fetch(): bileto entries from listing data


async def test_fetch_bileto_uses_listing_data_only() -> None:
    fake = FakeFetch(vitoria_routes())
    adapter = SymplaAdapter(fake)
    urls = [u async for u in adapter.discover(resolve("vitoria"))]
    assert BILETO_URL in urls
    requests_before = len(fake.calls)

    rec = await adapter.fetch(BILETO_URL)
    assert len(fake.calls) == requests_before  # no page fetch for bileto
    assert rec.external_id == "bileto-115574"
    assert rec.title == "TERAPIA - com Marcelo Serrado"
    assert rec.starts_at == datetime(2026, 9, 26, 21, 0, tzinfo=UTC)
    assert rec.ends_at == datetime(2026, 9, 27, 22, 0, tzinfo=UTC)
    assert rec.organizer == "WB PRODUCOES"
    assert rec.latitude is not None and rec.longitude is not None
    assert rec.raw["bileto"] is True
    assert rec.raw["price_lots_pending"] is True
    listing = rec.raw["listing"]
    assert isinstance(listing, dict)
    assert "email" not in listing["organizer"]


async def test_fetch_bileto_without_discover_is_an_error() -> None:
    with pytest.raises(ValueError, match="listing data only"):
        await SymplaAdapter(FakeFetch({})).fetch(BILETO_URL)


# --- tickets endpoint


def test_parse_tickets_walks_groups_and_skips_hidden_or_closed() -> None:
    body = json.dumps(
        {
            "tickets": [
                {"name": "Ingresso", "isFree": True, "salePriceMonetary": {"integer": 0}},
                {"name": "Hidden", "show": False, "salePriceMonetary": {"integer": 100}},
                {"name": "Old", "status": "CLOSED", "salePriceMonetary": {"integer": 100}},
            ],
            "groups": [
                {
                    "name": "Lote 2",
                    "tickets": [{"name": "Pista", "salePriceMonetary": {"integer": 2399}}],
                    "subgroups": [
                        {"tickets": [{"name": "VIP", "salePriceMonetary": {"integer": 6050}}]}
                    ],
                }
            ],
        }
    )
    assert parse_tickets(body) == [
        PriceLot(name="Ingresso", amount=Decimal(0)),
        PriceLot(name="Pista", amount=Decimal("23.99")),
        PriceLot(name="VIP", amount=Decimal("60.50")),
    ]


def test_parse_tickets_fixture() -> None:
    lots = parse_tickets(fixture("tickets-3537075.json"))
    assert [lot.amount for lot in lots] == [Decimal("140.40"), Decimal("108.00")]
    assert all(lot.currency == "BRL" for lot in lots)
