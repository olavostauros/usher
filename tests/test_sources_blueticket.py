"""Offline parsing tests for the BlueTicket adapter against sanitized fixtures."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evcrawl.pipeline.fetch import FetchError
from evcrawl.pipeline.normalize import map_category
from evcrawl.sources.base import PriceLot
from evcrawl.sources.blueticket import (
    DETAIL_URL,
    LISTING_URL,
    PRODUCTS_URL,
    BlueTicketAdapter,
    category_slug,
    city_names,
    parse_listing,
    parse_products,
    record_from_detail,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "blueticket"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

EVENT_ID = "41709"
EVENT_URL = f"https://www.blueticket.com.br/evento/{EVENT_ID}"
LISTING = LISTING_URL.format(state="ES")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetch:
    """Serves fixtures by URL and records every request; raises FetchError for the rest."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, source: str, url: str) -> str:
        assert source == "blueticket"
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


def event_routes() -> dict[str, str]:
    return {
        DETAIL_URL.format(id=EVENT_ID): f"event-{EVENT_ID}.json",
        PRODUCTS_URL.format(id=EVENT_ID): f"products-{EVENT_ID}.json",
    }


# --- fixtures hygiene (AGENTS.md §10, spec task 3 done-criterion)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_email_but_the_redacted_one(path: Path) -> None:
    found = set(EMAIL.findall(path.read_text(encoding="utf-8")))
    assert found <= {"redacted@example.org"}


# --- helpers


def test_parse_listing_state_es() -> None:
    items = parse_listing(fixture("search-es.json"))
    assert len(items) == 42
    cities = {(i["cityName"], i["cityState"]) for i in items}
    assert cities == {("Vitória", "ES"), ("Vila Velha", "ES"), ("Cariacica", "ES")}
    ev = next(i for i in items if i["id"] == 41709)
    assert ev["name"] == "Festival Country & Pop ! 🎸🤠"
    assert ev["date"] == "2026-09-26 21:00:00"  # local wall time...
    assert ev["dateTs"] == 1790467200  # ...and its UTC epoch (00:00Z next day)
    assert ev["venue"] == "Clube ARCI"
    assert ev["category"] == "Shows"
    # search items carry none of these; fetch() needs the detail (ADR 0007)
    assert not {"description", "latitude", "address", "organizer"} & set(ev)


def test_parse_listing_rejects_other_shapes() -> None:
    assert parse_listing("[]") == []
    with pytest.raises(ValueError, match="not a list"):
        parse_listing('{"items": []}')


def test_category_slug_and_mapping() -> None:
    assert category_slug("Shows") == "shows"
    assert category_slug("Cursos e Workshops") == "cursos-e-workshops"
    assert category_slug("Exposições") == "exposicoes"
    assert category_slug("Stand Up") == "stand-up"
    assert category_slug(None) is None
    assert category_slug("") is None
    for name in ("Shows", "Teatros", "Infantil", "Baladas", "Cursos e Workshops", "Congressos"):
        assert map_category("blueticket", category_slug(name)) != "uncategorized"
    assert map_category("blueticket", None) == "uncategorized"


def test_city_names() -> None:
    assert city_names("vila-velha") == ["Vila Velha"]
    assert city_names("Vila Velha") == ["Vila Velha"]
    assert city_names("vila-velha-es") == ["Vila Velha"]
    assert city_names("vitoria") == ["Vitória"]
    assert city_names("Vitória") == ["Vitória"]
    assert len(city_names(None)) == 6
    with pytest.raises(ValueError, match="unknown city"):
        city_names("sao-paulo")


# --- discover()


async def test_discover_filters_the_state_listing_by_city() -> None:
    fake = FakeFetch({LISTING: "search-es.json"})
    urls = [u async for u in BlueTicketAdapter(fake).discover("vila-velha")]
    assert fake.calls == [LISTING]  # one request for the whole state
    assert urls == [
        "https://www.blueticket.com.br/evento/41709",
        "https://www.blueticket.com.br/evento/41523",
        "https://www.blueticket.com.br/evento/41829",
        "https://www.blueticket.com.br/evento/41827",
    ]


async def test_discover_all_cities_covers_greater_vitoria_only() -> None:
    page = json.loads(fixture("search-es.json"))
    page[0]["cityName"] = "Linhares"  # in ES but outside Greater Vitória
    page[1]["cityState"] = "RJ"  # namesake city elsewhere
    page.append(dict(page[2], id=page[2]["id"]))  # duplicate id

    async def fake(source: str, url: str) -> str:
        return json.dumps(page)

    urls = [u async for u in BlueTicketAdapter(fake).discover(None)]
    assert len(urls) == 40 == len(set(urls))


async def test_discover_handles_an_unavailable_or_empty_listing() -> None:
    assert [u async for u in BlueTicketAdapter(FakeFetch({})).discover("vitoria")] == []

    async def empty(source: str, url: str) -> str:
        return "[]"

    assert [u async for u in BlueTicketAdapter(empty).discover("vitoria")] == []


# --- fetch(): detail + products


async def test_fetch_parses_detail_and_products() -> None:
    fake = FakeFetch(event_routes())
    rec = await BlueTicketAdapter(fake).fetch(EVENT_URL)
    assert fake.calls == [DETAIL_URL.format(id=EVENT_ID), PRODUCTS_URL.format(id=EVENT_ID)]

    assert rec.source == "blueticket"
    assert rec.external_id == EVENT_ID
    assert str(rec.source_url) == EVENT_URL
    assert rec.title == "Festival Country & Pop ! 🎸🤠"
    assert rec.starts_at == datetime(2026, 9, 27, 0, 0, tzinfo=UTC)  # 21:00 in São Paulo
    assert rec.ends_at is None
    assert rec.timezone == "America/Sao_Paulo"
    assert rec.venue_name == "Clube ARCI"
    assert rec.address == "Praça Assis chateaubriand,60 Ibes - Vila Velha - cep:29.108.630"
    assert rec.city == "Vila Velha"
    assert rec.state == "ES"
    assert rec.latitude is None and rec.longitude is None  # 0/0 in the API -> geocoder
    assert rec.organizer == "Clube ARCI"
    assert rec.category == "shows"
    assert rec.description is not None
    assert rec.description.startswith("📅 SÁBADO, 26 DE SETEMBRO")
    assert "<p>" not in rec.description and "\n" in rec.description
    assert str(rec.image_url).startswith("https://cdn.blueticket.com.br/images/imagens/full/")
    assert rec.cancelled is False and rec.closed is False
    assert rec.raw["products_fetched"] is True
    assert "price_lots_pending" not in rec.raw
    assert rec.price_lots == [
        PriceLot(name="Ingresso - Meia-entrada", amount=Decimal("27.50")),
        PriceLot(name="Ingresso - Meia Solidária Promocional", amount=Decimal("29.70")),
        PriceLot(name="Ingresso - Inteira", amount=Decimal("55.00")),
        PriceLot(name="Mesa - 4 ingressos (1° Piso) - 4 ingressos", amount=Decimal("132.00")),
    ]


def test_parse_products_skips_closed_lots_and_non_tickets() -> None:
    body = json.dumps(
        {
            "sectors": [
                {
                    "codigo": 0,
                    "nome": "",
                    "products": [
                        {
                            "nome": "Pista",
                            "nome_tipo": "Inteira",
                            "valor": 40,
                            "valor_conveniencia": 4,
                        },
                        {
                            "nome": "Pista",
                            "nome_tipo": "Pista",
                            "valor": 0,
                            "valor_conveniencia": 0,
                        },
                        {"nome": "Antigo", "nome_tipo": "Inteira", "valor": 30, "encerrado": 1},
                        {"nome": "Esgotado", "nome_tipo": "Inteira", "valor": 35, "esgotado": 1},
                        {"nome": "Estacionamento", "tipo_produto": "P", "valor": 20},
                        {"nome": "Sem preço", "nome_tipo": "Inteira"},
                    ],
                }
            ]
        }
    )
    assert parse_products(body) == [
        PriceLot(name="Pista - Inteira", amount=Decimal("44.00")),
        PriceLot(name="Pista", amount=Decimal("0.00")),
        PriceLot(name="Esgotado - Inteira", amount=Decimal("35.00")),
    ]
    assert parse_products('{"sectors": []}') == []


async def test_fetch_raw_strips_organizer_description_and_tracking() -> None:
    detail = json.loads(fixture(f"event-{EVENT_ID}.json"))
    assert "description" in detail["organizer"] and detail["integrations"]

    rec = await BlueTicketAdapter(FakeFetch(event_routes())).fetch(EVENT_URL)
    event = rec.raw["event"]
    assert isinstance(event, dict)
    assert "description" not in event["organizer"]
    assert "integrations" not in event
    assert "@" not in json.dumps(rec.raw, ensure_ascii=False)


async def test_fetch_marks_lots_pending_when_products_fail() -> None:
    fake = FakeFetch({DETAIL_URL.format(id=EVENT_ID): f"event-{EVENT_ID}.json"})
    rec = await BlueTicketAdapter(fake).fetch(EVENT_URL)
    assert rec.price_lots == []
    assert rec.raw["price_lots_pending"] is True
    assert rec.raw["products_fetched"] is False


async def test_fetch_accepts_the_slug_form_and_normalizes_it() -> None:
    fake = FakeFetch(event_routes())
    rec = await BlueTicketAdapter(fake).fetch(f"{EVENT_URL}/festival-country-pop-")
    assert str(rec.source_url) == EVENT_URL


def test_record_uses_coordinates_and_cancellation_when_present() -> None:
    detail = json.loads(fixture(f"event-{EVENT_ID}.json"))
    detail["venue"]["latitude"] = -20.3399
    detail["venue"]["longitude"] = -40.2926
    detail["sale"]["isCancelled"] = True
    detail["venue"]["type"] = "online"
    rec = record_from_detail(EVENT_URL, json.dumps(detail), [])
    assert rec.latitude == pytest.approx(-20.3399)
    assert rec.longitude == pytest.approx(-40.2926)
    assert rec.cancelled is True
    assert rec.raw["online"] is True

    detail["dates"]["dateTs"] = 0
    with pytest.raises(ValueError, match="dateTs"):
        record_from_detail(EVENT_URL, json.dumps(detail), [])


def test_record_rejects_a_response_without_id() -> None:
    with pytest.raises(ValueError, match="event id"):
        record_from_detail(EVENT_URL, '{"info": {"name": "x"}}', [])


async def test_fetch_rejects_foreign_urls() -> None:
    with pytest.raises(ValueError, match="not a BlueTicket event URL"):
        await BlueTicketAdapter(FakeFetch({})).fetch("https://example.org/evento/41709")
