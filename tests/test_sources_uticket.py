"""Offline parsing tests for the uTicket adapter against sanitized fixtures."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evcrawl.cities import for_source, resolve
from evcrawl.pipeline.fetch import FetchError
from evcrawl.sources.base import PriceLot
from evcrawl.sources.uticket import (
    EVENT_INFO_URL,
    PAGE_SIZE,
    SEARCH_URL,
    TICKETS_URL,
    UTicketAdapter,
    description_text,
    parse_place,
    parse_search,
    parse_tickets,
    record_from_event,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "uticket"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

EVENT_ID = "01M4IIJK3JGTH9"
EVENT_URL = f"https://uticket.com.br/event/{EVENT_ID}"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)  # fixtures were recorded on this day


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetch:
    """Serves fixtures by URL and records every request; raises FetchError for the rest."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, source: str, url: str) -> str:
        assert source == "uticket"
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


def search_url(city: str, skip: int) -> str:
    return SEARCH_URL.format(limit=PAGE_SIZE, skip=skip, city=city)


def vila_velha_routes() -> dict[str, str]:
    return {
        search_url("Vila%20Velha", 0): "search-vila-velha-p1.json",
        search_url("Vila%20Velha", PAGE_SIZE): "search-vila-velha-empty.json",
    }


def event_routes() -> dict[str, str]:
    return {
        EVENT_INFO_URL.format(id=EVENT_ID): f"event-{EVENT_ID}.json",
        TICKETS_URL.format(id=EVENT_ID): f"tickets-{EVENT_ID}.json",
    }


# --- fixtures hygiene (AGENTS.md §10, spec task 1 done-criterion)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_email_but_the_redacted_one(path: Path) -> None:
    found = set(EMAIL.findall(path.read_text(encoding="utf-8")))
    assert found <= {"redacted@example.org"}


# --- helpers


def test_parse_search_page_1() -> None:
    items, count = parse_search(fixture("search-vila-velha-p1.json"))
    assert count == 6 and len(items) == 6
    by_id = {i["id"]: i for i in items}
    assert by_id[EVENT_ID]["name"] == "Eclipse Festival"
    assert by_id[EVENT_ID]["startDate"] == "2026-10-31T18:00:00Z"
    assert by_id[EVENT_ID]["place"] == "Sítio Marinho - Vila Velha-ES"
    assert by_id[EVENT_ID]["minPrice"] == 30
    # search items carry none of these; fetch() needs eventinfo (ADR 0006)
    assert not {"description", "position", "address", "category"} & set(by_id[EVENT_ID])


def test_parse_search_past_the_end_is_empty() -> None:
    assert parse_search(fixture("search-vila-velha-empty.json")) == ([], 6)


def test_parse_search_rejects_other_shapes() -> None:
    with pytest.raises(ValueError, match="items"):
        parse_search('{"count": 1}')
    with pytest.raises(ValueError, match="items"):
        parse_search("[]")


def test_parse_place() -> None:
    assert parse_place("The Point - Vila Velha-ES") == ("The Point", "Vila Velha", "ES")
    assert parse_place("Folha Seca - Campos dos Goytacazes-RJ") == (
        "Folha Seca",
        "Campos dos Goytacazes",
        "RJ",
    )
    assert parse_place("Rua Angelo Borgo, 393 - Vila Velha-ES") == (
        "Rua Angelo Borgo, 393",
        "Vila Velha",
        "ES",
    )
    assert parse_place("Jardim vila velha - Vila Velha-ES") == (
        "Jardim vila velha",
        "Vila Velha",
        "ES",
    )
    assert parse_place("Somewhere") == ("Somewhere", None, None)
    assert parse_place(None) == (None, None, None)
    assert parse_place("") == (None, None, None)


def test_description_text_decodes_the_double_encoded_html() -> None:
    assert description_text('"<p>Olá</p><p><br></p><p>Line&nbsp;two</p>"') == "Olá\nLine two"
    assert description_text("<p>plain html</p>") == "plain html"
    assert description_text('"<p></p>"') is None
    assert description_text("") is None
    assert description_text(None) is None


def test_registry_keys_are_the_api_city_names() -> None:
    assert resolve("vila-velha").key("uticket") == "Vila Velha"
    assert resolve("Vila Velha").key("uticket") == "Vila Velha"
    assert resolve("vila-velha-es").key("uticket") == "Vila Velha"
    assert resolve("Vitória").key("uticket") == "Vitória"
    assert [c.key("uticket") for c in for_source("uticket")] == [
        "Vitória",
        "Vila Velha",
        "Serra",
        "Cariacica",
        "Viana",
        "Guarapari",
    ]


# --- discover()


async def test_discover_paginates_until_count_is_reached() -> None:
    fake = FakeFetch(vila_velha_routes())
    urls = [u async for u in UTicketAdapter(fake).discover(resolve("vila-velha"))]
    # 6 items < PAGE_SIZE on page 1, so the count rule ends discovery in one request
    assert fake.calls == [search_url("Vila%20Velha", 0)]
    assert len(urls) == 6 == len(set(urls))
    assert EVENT_URL in urls
    assert all(re.fullmatch(r"https://uticket\.com\.br/event/[A-Z0-9]+", u) for u in urls)


async def test_discover_stops_on_an_empty_page() -> None:
    page = json.loads(fixture("search-vila-velha-p1.json"))
    page["count"] = 1000  # claims more than it serves; the empty page must end it

    async def fake(source: str, url: str) -> str:
        return (
            json.dumps(page)
            if url == search_url("Vila%20Velha", 0)
            else '{"count":1000,"items":[]}'
        )

    urls = [u async for u in UTicketAdapter(fake).discover(resolve("vila-velha"))]
    assert len(urls) == 6


async def test_discover_skips_another_states_namesake_city() -> None:
    page = json.loads(fixture("search-vila-velha-p1.json"))
    page["items"][0]["place"] = "Folha Seca - Campos dos Goytacazes-RJ"
    page["items"][1]["place"] = "Sem padrão"  # unparsable place: kept, fetch() decides

    async def fake(source: str, url: str) -> str:
        return json.dumps(page)

    urls = [u async for u in UTicketAdapter(fake).discover(resolve("vila-velha"))]
    assert len(urls) == 5
    assert EVENT_URL in urls  # Eclipse is items[1]: unparsable place, still yielded
    assert "https://uticket.com.br/event/01MD0STNR0V3II" not in urls


async def test_discover_keeps_only_the_requested_capitals_state() -> None:
    # ``city=Macapá`` is a fuzzy match: the recon saw Macaé-RJ and Marabá-PA items.
    page = json.loads(fixture("search-vila-velha-p1.json"))
    places = ["Praça - Macaé-RJ", "Centro - Marabá-PA"] + ["Teatro - Macapá-AP"] * 4
    for item, place in zip(page["items"], places, strict=True):
        item["place"] = place

    async def fake(source: str, url: str) -> str:
        assert "city=Macap%C3%A1" in url
        return json.dumps(page)

    urls = [u async for u in UTicketAdapter(fake).discover(resolve("macapa"))]
    assert len(urls) == 4  # the two namesakes are dropped, the four AP items kept


async def test_discover_logs_and_skips_an_unavailable_city() -> None:
    fake = FakeFetch(vila_velha_routes())  # every other city 404s
    urls = [u async for u in UTicketAdapter(fake).discover(None)]
    assert len(urls) == 6
    cities = {u.split("city=")[1].split("&")[0] for u in fake.calls}
    assert cities == {"Vit%C3%B3ria", "Vila%20Velha", "Serra", "Cariacica", "Viana", "Guarapari"}


# --- fetch(): eventinfo + tickettype


async def test_fetch_parses_eventinfo_and_tickets() -> None:
    fake = FakeFetch(event_routes())
    rec = await UTicketAdapter(fake).fetch(EVENT_URL)
    assert fake.calls == [EVENT_INFO_URL.format(id=EVENT_ID), TICKETS_URL.format(id=EVENT_ID)]

    assert rec.source == "uticket"
    assert rec.external_id == EVENT_ID
    assert str(rec.source_url) == EVENT_URL
    assert rec.title == "Eclipse Festival"
    assert rec.starts_at == datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
    assert rec.ends_at == datetime(2026, 11, 1, 19, 0, tzinfo=UTC)
    assert rec.timezone == "America/Sao_Paulo"
    assert rec.venue_name == "Sítio Marinho"
    assert rec.address == "Jardim vila velha - Vila Velha-ES"
    assert rec.city == "Vila Velha"
    assert rec.state == "ES"
    assert rec.latitude == pytest.approx(-20.477737)
    assert rec.longitude == pytest.approx(-40.438842)
    assert rec.organizer == "Produtora Exemplo"
    assert rec.category is None  # the API has no category; normalize() -> uncategorized
    assert rec.description is not None
    assert rec.description.startswith("ECLIPSE FESTIVAL")
    assert "<p>" not in rec.description and "\n" in rec.description
    assert str(rec.image_url) == f"https://img.uticket.com.br/event/{EVENT_ID}/l"
    assert rec.cancelled is False and rec.closed is False
    assert rec.raw["tickets_fetched"] is True
    assert "price_lots_pending" not in rec.raw


def test_record_price_lots_are_buyer_prices_in_reais() -> None:
    lots = parse_tickets(fixture(f"tickets-{EVENT_ID}.json"), now=NOW)
    assert lots == [
        PriceLot(name="INGRESSO ÚNICO - 1º LOTE", amount=Decimal("55.00")),
        PriceLot(name="INGRESSO DUPLO - 1º LOTE", amount=Decimal("88.00")),
        PriceLot(name="INGRESSO TRIPLO - 1º LOTE", amount=Decimal("143.00")),
        PriceLot(name="KIT COMPLETO - 1º LOTE", amount=Decimal("33.00")),
    ]
    assert all(lot.currency == "BRL" for lot in lots)


def test_parse_tickets_skips_restricted_and_ended_lots() -> None:
    body = json.dumps(
        {
            "ticketTypes": [
                {"fullname": "Pista", "price": 2000, "taxPrice": 200},
                {"fullname": "Cortesia", "price": 0, "taxPrice": 0},
                {"fullname": "Link privado", "price": 100, "restricted": True},
                {"fullname": "1º lote", "price": 100, "sellEnd": "2026-01-01T00:00:00Z"},
                {"fullname": "Sem preço"},
            ]
        }
    )
    assert parse_tickets(body, now=NOW) == [
        PriceLot(name="Pista", amount=Decimal("22.00")),
        PriceLot(name="Cortesia", amount=Decimal(0)),
    ]
    assert parse_tickets('{"ticketTypes": []}', now=NOW) == []


async def test_fetch_raw_strips_the_organizer_email() -> None:
    api_event = json.loads(fixture(f"event-{EVENT_ID}.json"))
    assert api_event["userEmail"] == "redacted@example.org"  # present in the response...

    rec = await UTicketAdapter(FakeFetch(event_routes())).fetch(EVENT_URL)
    event = rec.raw["event"]
    assert isinstance(event, dict)
    assert "userEmail" not in event  # ...never stored
    assert event["UserName"] == "Produtora Exemplo"
    assert "@" not in json.dumps(rec.raw, ensure_ascii=False)


async def test_fetch_marks_lots_pending_when_tickets_fail() -> None:
    fake = FakeFetch({EVENT_INFO_URL.format(id=EVENT_ID): f"event-{EVENT_ID}.json"})
    rec = await UTicketAdapter(fake).fetch(EVENT_URL)
    assert rec.price_lots == []
    assert rec.raw["price_lots_pending"] is True
    assert rec.raw["tickets_fetched"] is False


def test_record_uses_the_widest_schedule_span() -> None:
    event = json.loads(fixture(f"event-{EVENT_ID}.json"))
    event["schedules"] = [
        {"start": "2026-10-31T18:00:00Z", "end": "2026-11-01T02:00:00Z"},
        {"start": "2026-11-01T14:00:00Z", "end": "2026-11-01T19:00:00Z"},
    ]
    rec = record_from_event(EVENT_URL, json.dumps(event), [])
    assert rec.starts_at == datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
    assert rec.ends_at == datetime(2026, 11, 1, 19, 0, tzinfo=UTC)

    event["schedules"] = []
    event["sessions"] = []
    with pytest.raises(ValueError, match="schedule"):
        record_from_event(EVENT_URL, json.dumps(event), [])


def test_record_rejects_a_response_without_id() -> None:
    with pytest.raises(ValueError, match="event id"):
        record_from_event(EVENT_URL, '{"name": "x"}', [])


async def test_fetch_rejects_foreign_urls() -> None:
    with pytest.raises(ValueError, match="not a uTicket event URL"):
        await UTicketAdapter(FakeFetch({})).fetch("https://example.org/event/01M4IIJK3JGTH9")
