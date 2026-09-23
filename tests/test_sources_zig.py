"""Offline parsing tests for the Zig adapter against sanitized fixtures (ADR 0009)."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evcrawl.cities import City, for_source, resolve
from evcrawl.pipeline.fetch import FetchError
from evcrawl.pipeline.normalize import map_category, normalize
from evcrawl.sources.base import Discovered, PriceLot
from evcrawl.sources.zig import (
    FEED_URL,
    ZigAdapter,
    category_slug,
    parse_feed,
    parse_next_data,
    parse_tickets,
    record_from_page,
    strip_contact,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "zig"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOW = datetime(2026, 9, 21, 18, 0, tzinfo=UTC)

VINTAGE_URL = "https://zig.tickets/eventos/vintage-vila-velha"
SAMBA_URL = "https://zig.tickets/eventos/samba-em-vix-pagode-da-garagem"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetch:
    """Serves fixtures by URL and records every request; raises FetchError for the rest."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, source: str, url: str, *, json: object | None = None) -> str:
        assert source == "zig" and json is None  # every Zig request is a GET
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


def routes(feed: bool = True) -> dict[str, str]:
    out = {
        VINTAGE_URL: "event-vintage-vila-velha.html",
        SAMBA_URL: "event-samba-em-vix-pagode-da-garagem.html",
    }
    if feed:
        out[FEED_URL] = "new-events.json"
    return out


def feed_item(slug: str) -> dict[str, object]:
    return next(i for i in parse_feed(fixture("new-events.json")) if i["slug"] == slug)


# --- fixtures hygiene (AGENTS.md §10)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_email(path: Path) -> None:
    assert set(EMAIL.findall(path.read_text(encoding="utf-8"))) <= {"redacted@example.org"}


def test_feed_fixture_has_no_organization_contact() -> None:
    for item in parse_feed(fixture("new-events.json")):
        organization = item["organization"]
        assert isinstance(organization, dict)
        assert not organization.get("phone") and not organization.get("email")


# --- helpers


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Shows e Festivais", "shows-e-festivais"),
        ("Teatro, Museus e Cultura", "teatro-museus-e-cultura"),
        ("Parques, Passeios e Tours", "parques-passeios-e-tours"),
        ("Eletrônica", "eletronica"),
        ("", None),
        (None, None),
    ],
)
def test_category_slug(name: str | None, slug: str | None) -> None:
    assert category_slug(name) == slug
    if slug and slug != "eletronica":
        assert map_category("zig", slug) != "uncategorized" or slug == "eventos-privados"


def test_parse_feed() -> None:
    items = parse_feed(fixture("new-events.json"))
    assert len(items) == 46
    es = [i for i in items if i["event_location"]["state"] == "ES"]
    assert len(es) == 42
    item = feed_item("vintage-vila-velha")
    assert item["id"] == 27550 and item["event_location"]["city"] == "Vila Velha"
    assert item["start_date"] == "2026-11-07T15:00:00.000-03:00"
    assert [c["name"] for c in item["event_categories"]] == ["Shows e Festivais"]
    with pytest.raises(ValueError):
        parse_feed(json.dumps({"states": []}))


def test_strip_contact_reduces_organization() -> None:
    item = {
        "id": 1,
        "organization": {"id": 9, "name": "Org", "slug": "org", "phone": "x", "email": "y"},
    }
    clean = strip_contact(item)
    assert clean["organization"] == {"id": 9, "name": "Org", "slug": "org"}
    assert "phone" in item["organization"]  # the original is untouched


def test_parse_next_data() -> None:
    props = parse_next_data(fixture("event-vintage-vila-velha.html"))
    assert set(props) >= {"event", "tickets", "slug"}
    assert "messages" not in props  # dropped from the fixture
    with pytest.raises(ValueError):
        parse_next_data("<html></html>")


def test_parse_tickets_adds_fee_and_prefixes_sector() -> None:
    tickets = {
        "availables": [
            {"name": "Pista [Meia] 3º Lote", "sector_name": "Pista", "value": 160, "fee": 19.2,
             "public": 1},
            {"name": "Inteira 1º Lote", "sector_name": "Camarote", "value": 100, "fee": 0,
             "public": 1},
            {"name": "Convidado", "sector_name": "Pista", "value": 0, "fee": 0, "public": 0},
            {"name": "Gratuito", "sector_name": "", "value": 0, "fee": 0, "public": 1},
            {"name": "Sem valor", "sector_name": "", "value": None, "public": 1},
        ]
    }  # fmt: skip
    assert parse_tickets(tickets) == [
        PriceLot(name="Pista [Meia] 3º Lote", amount=Decimal("179.20")),
        PriceLot(name="Camarote - Inteira 1º Lote", amount=Decimal("100.00")),
        PriceLot(name="Gratuito", amount=Decimal("0.00")),
    ]
    assert parse_tickets(None) == [] and parse_tickets({"availables": []}) == []


def test_registry_keys_are_the_feed_city_names() -> None:
    assert len(for_source("zig")) == 6
    assert resolve("vila-velha").key("zig") == "Vila Velha"
    assert resolve("Vitória").key("zig") == "Vitória"
    assert resolve("guarapari-es").key("zig") == "Guarapari"
    with pytest.raises(ValueError, match="unknown city"):
        resolve("linhares")


def test_unknown_zone_falls_back_to_the_states_zone() -> None:
    # A zone the tzdata does not know (``Brazil/*`` aliases in the image, ADR
    # 0009) falls back to the UF's zone, not to Brasília's.
    html = fixture("event-vintage-vila-velha.html")
    assert html.count('"timezone": "America/Sao_Paulo"') == 1 and html.count('"state": "ES"') == 1
    relocated = html.replace('"timezone": "America/Sao_Paulo"', '"timezone": "Brazil/Nowhere"')
    assert record_from_page(VINTAGE_URL, relocated).timezone == "America/Sao_Paulo"  # ES
    relocated = relocated.replace('"state": "ES"', '"state": "PE"')
    assert record_from_page(VINTAGE_URL, relocated).timezone == "America/Recife"


# --- record_from_page


def test_record_from_page_full() -> None:
    record = record_from_page(
        VINTAGE_URL, fixture("event-vintage-vila-velha.html"), feed_item("vintage-vila-velha")
    )
    assert record.source == "zig" and record.external_id == "27550"
    assert str(record.source_url) == VINTAGE_URL
    assert record.title == "Vintage Culture @ Shopping Vila Velha"
    assert record.starts_at == datetime(2026, 11, 7, 18, 0, tzinfo=UTC)  # 15:00 -03:00
    assert record.ends_at == datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
    assert record.timezone == "America/Sao_Paulo"
    assert record.venue_name == "Shopping Vila Velha"
    assert (
        record.address
        == "Rua Luciano das Neves, 2418 - Divino Espírito Santo, Vila Velha, ES - 29107-900"
    )
    assert (record.city, record.state) == ("Vila Velha", "ES")
    assert record.latitude is None and record.longitude is None
    assert record.organizer == "BRAVA ENTRETENIMENTO LTDA"
    assert record.category == "shows-e-festivais"
    assert map_category("zig", record.category) == "music"
    assert record.description is not None
    assert record.description.startswith("10 ANOS DEPOIS ELE ESTÁ DE VOLTA A VILA VELHA - ES")
    assert "<" not in record.description
    assert str(record.image_url).startswith("https://superticket-assets.s3.amazonaws.com/eventos/")
    assert record.cancelled is False and record.closed is False
    lots = {lot.name: lot.amount for lot in record.price_lots}
    assert lots["Pista [Meia] 3º Lote"] == Decimal("179.20")  # 160 + 19.2
    assert lots["Pista [Inteira] 3º Lote"] == Decimal("358.40")
    assert len(lots) == 8
    assert "price_lots_pending" not in record.raw
    event = record.raw["event"]
    assert isinstance(event, dict) and event["slug"] == "vintage-vila-velha"
    for key in ("description", "json_ld", "fb_pixel_id", "g_analytics_code", "gateway"):
        assert key not in event
    listing = record.raw["listing"]
    assert isinstance(listing, dict)
    assert set(listing["organization"]) == {"id", "name", "slug"}
    assert isinstance(record.raw["tickets"], list) and len(record.raw["tickets"]) == 8


def test_record_from_page_without_listing_has_no_category() -> None:
    record = record_from_page(SAMBA_URL, fixture("event-samba-em-vix-pagode-da-garagem.html"))
    assert record.external_id == "28128"
    assert record.title == "Samba em Vix | Pagode da Garagem"
    assert record.category is None and record.raw["listing"] is None
    assert record.venue_name == "Clube de Pesca" and record.city == "Vitória"
    assert record.starts_at == datetime(2026, 10, 3, 19, 0, tzinfo=UTC)  # 16:00 -03:00
    assert record.price_lots and all(lot.amount > 0 for lot in record.price_lots)


def test_normalize_end_to_end() -> None:
    record = record_from_page(
        VINTAGE_URL, fixture("event-vintage-vila-velha.html"), feed_item("vintage-vila-velha")
    )
    n = normalize(record, now=NOW)
    assert n.starts_at == datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    assert n.category == "music"
    assert n.venue is not None and n.venue.postal_code == "29107-900"
    assert n.organizer is not None and n.organizer.name == "BRAVA ENTRETENIMENTO LTDA"


def test_record_from_page_rejects_pages_without_event() -> None:
    with pytest.raises(ValueError):
        record_from_page(VINTAGE_URL, "<html><body>nothing</body></html>")


# --- discover / fetch


async def discovered(adapter: ZigAdapter, city: City | None) -> list[Discovered]:
    items = [d async for d in adapter.discover(city)]
    assert all(isinstance(d, Discovered) for d in items)  # every yield carries a fingerprint
    return [d for d in items if isinstance(d, Discovered)]


async def collect(adapter: ZigAdapter, city: City | None) -> list[str]:
    return [d.url for d in await discovered(adapter, city)]


async def test_discovered_ids_match_the_fetched_records() -> None:
    """ADR 0013: the crawl finds the stored listing by the id discover() carries."""
    adapter = ZigAdapter(FakeFetch(routes()))
    item = next(d for d in await discovered(adapter, resolve("vila-velha")) if d.url == VINTAGE_URL)
    assert item.external_id == str(feed_item("vintage-vila-velha")["id"])
    assert (await adapter.fetch(VINTAGE_URL)).external_id == item.external_id


async def test_discover_fingerprint_ignores_the_organization_block() -> None:
    data = json.loads(fixture("new-events.json"))

    async def fingerprint_of(body: dict[str, object]) -> str:
        text = json.dumps(body)

        async def fake(source: str, url: str, *, json: object | None = None) -> str:
            return text

        items = await discovered(ZigAdapter(fake), resolve("vila-velha"))
        return next(d.fingerprint for d in items if d.url == VINTAGE_URL)

    def vintage(body: dict[str, object]) -> dict[str, object]:
        events = body["events"]
        assert isinstance(events, list)
        return next(i for i in events if i["slug"] == "vintage-vila-velha")

    original = await fingerprint_of(data)
    volatile = json.loads(json.dumps(data))
    vintage(volatile)["organization"]["updated_at"] = "2026-09-23T00:00:00.000-03:00"  # type: ignore[index]
    vintage(volatile)["verification_code"] = "abc"
    assert await fingerprint_of(volatile) == original
    changed = json.loads(json.dumps(data))
    vintage(changed)["start_date"] = "2026-11-08T18:00:00.000-03:00"
    assert await fingerprint_of(changed) != original


async def test_discover_filters_state_and_city() -> None:
    fake = FakeFetch(routes())
    urls = await collect(ZigAdapter(fake), resolve("vila-velha"))
    assert fake.calls == [FEED_URL]
    assert len(urls) == 6 and VINTAGE_URL in urls
    assert all(u.startswith("https://zig.tickets/eventos/") for u in urls)


async def test_discover_all_of_greater_vitoria_skips_other_cities_and_states() -> None:
    fake = FakeFetch(routes())
    urls = await collect(ZigAdapter(fake), None)
    assert len(urls) == 33  # 42 ES items minus the 9 outside Greater Vitória; no other state
    assert len(set(urls)) == 33 and SAMBA_URL in urls
    assert "https://zig.tickets/eventos/carnaval-exodus" not in urls  # PR


async def test_discover_skips_closed_and_past_events() -> None:
    data = json.loads(fixture("new-events.json"))
    for item in data["events"]:
        if item["slug"] == "vintage-vila-velha":
            item["is_closed"] = True
        elif item["slug"] == "samba-em-vix-pagode-da-garagem":
            item["start_date"] = "2020-01-01T10:00:00.000-03:00"
            item["end_date"] = "2020-01-01T12:00:00.000-03:00"
    body = json.dumps(data)

    async def fake(source: str, url: str, *, json: object | None = None) -> str:
        return body

    urls = await collect(ZigAdapter(fake), None)
    assert VINTAGE_URL not in urls and SAMBA_URL not in urls and len(urls) == 31


async def test_discover_yields_a_disabled_capital_only_when_asked() -> None:
    data = json.loads(fixture("new-events.json"))
    item = next(i for i in data["events"] if i["slug"] == "vintage-vila-velha")
    item["event_location"].update(state="am", city="Manaus")  # dirty case, matched folded
    body = json.dumps(data)

    async def fake(source: str, url: str, *, json: object | None = None) -> str:
        return body

    adapter = ZigAdapter(fake)
    assert await collect(adapter, resolve("manaus")) == [VINTAGE_URL]
    assert VINTAGE_URL not in await collect(adapter, None)  # Manaus is not enabled yet


async def test_discover_stops_quietly_when_the_feed_fails() -> None:
    fake = FakeFetch(routes(feed=False))
    assert await collect(ZigAdapter(fake), None) == []


async def test_feed_is_loaded_once_per_adapter() -> None:
    fake = FakeFetch(routes())
    adapter = ZigAdapter(fake)
    await collect(adapter, resolve("vila-velha"))
    record = await adapter.fetch(VINTAGE_URL)
    assert record.category == "shows-e-festivais"
    assert fake.calls == [FEED_URL, VINTAGE_URL]  # the feed is not fetched again


async def test_fetch_alone_loads_the_feed_for_the_category() -> None:
    fake = FakeFetch(routes())
    record = await ZigAdapter(fake).fetch(SAMBA_URL)  # the hourly refresh path
    assert fake.calls == [FEED_URL, SAMBA_URL]
    assert record.category == "shows-e-festivais"
    assert map_category("zig", record.category) == "music"


async def test_fetch_without_feed_still_returns_the_record() -> None:
    fake = FakeFetch(routes(feed=False))
    record = await ZigAdapter(fake).fetch(VINTAGE_URL)
    assert record.external_id == "27550" and record.category is None
    with pytest.raises(ValueError, match="not a Zig event URL"):
        await ZigAdapter(fake).fetch("https://zig.tickets/evento/1")


async def test_fetch_propagates_fetch_errors() -> None:
    fake = FakeFetch({FEED_URL: "new-events.json"})
    with pytest.raises(FetchError):
        await ZigAdapter(fake).fetch(VINTAGE_URL)
