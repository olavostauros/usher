"""Offline parsing tests for the TicketBolt adapter against sanitized fixtures (ADR 0008)."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from evcrawl.cities import City, for_source, resolve
from evcrawl.pipeline.fetch import FetchError
from evcrawl.pipeline.normalize import map_category, normalize
from evcrawl.sources.base import Discovered, PriceLot
from evcrawl.sources.ticketbolt import (
    LISTING_QUERY,
    QUERY_URL,
    TicketBoltAdapter,
    event_slug,
    flight_rows,
    flight_stream,
    is_html,
    listing_args,
    parse_event_page,
    parse_listing,
    parse_lots,
    query_body,
    record_from_page,
    split_location,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "ticketbolt"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOW = datetime(2026, 9, 21, 18, 0, tzinfo=UTC)

SOMOS_ROCK_URL = "https://www.ticketbolt.com/event/somos-rock"
LAGUN_URL = "https://www.ticketbolt.com/event/lagun-bin"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetch:
    """Serves fixtures by URL (GET) or by listing page (POST); records every request."""

    def __init__(self, routes: dict[str, str], pages: dict[int | None, str] | None = None) -> None:
        self.routes = routes
        self.pages = pages if pages is not None else {None: "query-getPublicEventsList.json"}
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, source: str, url: str, *, json: object | None = None) -> str:
        assert source == "ticketbolt"
        self.calls.append((url, json))
        if json is not None:
            assert url == QUERY_URL and isinstance(json, dict)
            assert json["path"] == LISTING_QUERY and json["format"] == "json"
            args = json["args"]
            assert isinstance(args, dict) and args["country"] == "BRA" and "now" in args
            page = args.get("page")
            if page not in self.pages:
                raise FetchError(url, status=500)
            return fixture(self.pages[page])
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


def routes() -> dict[str, str]:
    return {SOMOS_ROCK_URL: "event-somos-rock.html", LAGUN_URL: "event-lagun-bin.html"}


# --- fixtures hygiene (AGENTS.md §10)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_email(path: Path) -> None:
    assert set(EMAIL.findall(path.read_text(encoding="utf-8"))) <= {"redacted@example.org"}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_json_fixture_has_no_at_sign(path: Path) -> None:
    assert "@" not in path.read_text(encoding="utf-8")


def test_event_fixtures_carry_no_real_producer_name() -> None:
    for name in ("event-somos-rock.html", "event-lagun-bin.html", "event-somos-rock.rsc.txt"):
        detail, _ = parse_event_page(fixture(name))
        assert detail["event"]["producerName"] == "Produtora Exemplo"


# --- helpers


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("SOMOS ROCK", "somos-rock"),
        ("CARNAVAL DE VITÓRIA  2027  29/01", "carnaval-de-vitoria-2027-2901"),
        ("LAGUN - BIN", "lagun-bin"),
        ('BOTECO DA PANELA & BAR DA ZILDA "CONVIDAM"', "boteco-da-panela-bar-da-zilda-convidam"),
        ("2ª FEIJOADA DO GA40", "2-feijoada-do-ga40"),  # ª is not ASCII \w
        ("  --Açaí--  ", "acai"),
    ],
)
def test_event_slug_matches_the_site(name: str, slug: str) -> None:
    assert event_slug(name) == slug


def test_listing_args_and_query_body() -> None:
    args = listing_args(now=NOW)
    assert args == {"country": "BRA", "now": 1790013600000}  # 2026-09-21T18:00Z
    assert listing_args(3, now=NOW)["page"] == 3
    assert query_body(LISTING_QUERY, args) == {
        "path": "events:getPublicEventsList",
        "args": args,
        "format": "json",
    }


def test_parse_listing() -> None:
    items, has_next = parse_listing(fixture("query-getPublicEventsList.json"))
    assert has_next is False and len(items) == 24
    assert sum(1 for i in items if i["state"] == "ES") == 21
    ev = next(i for i in items if i["_id"] == "mx70htxeapzmtajj4ymmbqe0k98bdhds")
    assert ev["name"] == "SOMOS ROCK" and ev["city"] == "Vila Velha"
    assert ev["eventDate"] == 1793541600000  # 14:00 wall time serialized as UTC
    assert "slug" not in ev  # the adapter derives it from the name
    items, has_next = parse_listing(fixture("query-getPublicEventsList-p2.json"))
    assert has_next is True and len(items) == 5


def test_parse_listing_rejects_errors() -> None:
    with pytest.raises(ValueError, match="Server Error"):
        parse_listing(json.dumps({"status": "error", "errorMessage": "Server Error"}))
    with pytest.raises(ValueError):
        parse_listing(json.dumps({"status": "success", "value": {}}))


def test_flight_rows_decode_json_and_text_rows() -> None:
    rows = flight_rows(fixture("event-somos-rock.html"))
    assert rows["1"] == '"$Sreact.fragment"'
    assert rows["25"].startswith("Contato EXCLUSIVO")  # the T (text) row, byte-exact
    assert "initialEventDetail" in rows["5"]
    assert flight_rows("<html>no chunks</html>") == {}


def test_parse_event_page_and_description_reference() -> None:
    detail, rows = parse_event_page(fixture("event-somos-rock.html"))
    assert detail["event"]["_id"] == "mx70htxeapzmtajj4ymmbqe0k98bdhds"
    assert detail["event"]["description"] == "$25"
    assert rows["25"].startswith("Contato EXCLUSIVO")
    with pytest.raises(ValueError):
        parse_event_page("<html></html>")


# --- the RSC flight payload (``RSC: 1``, ADR 0008 addendum)


def test_is_html_tells_pages_from_flight_streams() -> None:
    assert is_html(fixture("event-somos-rock.html"))
    assert is_html("\n  <!DOCTYPE html><html></html>")
    assert not is_html(fixture("event-somos-rock.rsc.txt"))
    assert not is_html('1:"$Sreact.fragment"\n')
    assert not is_html("")


def test_flight_stream_is_the_same_from_the_page_and_from_the_payload() -> None:
    from_page = flight_rows(fixture("event-somos-rock.html"))
    from_payload = flight_rows(fixture("event-somos-rock.rsc.txt"))
    # Row ids differ between the two renders (the payload has no HTML shell),
    # but the description text row is byte-identical and the event row is the
    # same object (recorded hours apart: only the live counters moved).
    assert from_payload["24"] == from_page["25"]  # the T row
    assert '"initialEventDetail"' in from_payload["5"] and '"initialEventDetail"' in from_page["5"]
    raw = fixture("event-somos-rock.rsc.txt")
    assert flight_stream(raw) == raw.encode("utf-8")


def test_record_from_rsc_payload_equals_record_from_page() -> None:
    from_page = record_from_page(SOMOS_ROCK_URL, fixture("event-somos-rock.html"))
    from_payload = record_from_page(SOMOS_ROCK_URL, fixture("event-somos-rock.rsc.txt"))
    # The two fixtures were recorded hours apart; only live counters differ.
    assert from_payload.model_dump(exclude={"raw"}) == from_page.model_dump(exclude={"raw"})
    assert from_payload.description is not None
    assert from_payload.description.startswith("Contato EXCLUSIVO para aquisição de lounges")
    assert len(from_payload.price_lots) == len(from_page.price_lots) > 0

    def stable(raw: dict[str, object]) -> dict[str, object]:
        event = dict(raw["event"])  # type: ignore[call-overload]
        event.pop("visualizacoes", None)
        lots = [{k: v for k, v in lot.items() if k != "remainingQuantity"} for lot in raw["lots"]]  # type: ignore[union-attr]
        return {**raw, "event": event, "lots": lots}

    assert stable(from_payload.raw) == stable(from_page.raw)


def test_rsc_payload_without_event_is_rejected() -> None:
    with pytest.raises(ValueError, match="no initialEventDetail"):
        record_from_page(SOMOS_ROCK_URL, '1:"$Sreact.fragment"\n0:{"b":"x","f":[]}\n')


async def test_fetch_reads_the_rsc_payload_and_declares_the_header_for_the_site_only() -> None:
    fake = FakeFetch({SOMOS_ROCK_URL: "event-somos-rock.rsc.txt"})
    record = await TicketBoltAdapter(fake).fetch(SOMOS_ROCK_URL)
    assert record.external_id == "mx70htxeapzmtajj4ymmbqe0k98bdhds"
    assert record.organizer == "Produtora Exemplo"
    assert fake.calls == [(SOMOS_ROCK_URL, None)]
    # The header goes to the event pages' host only; the Convex host gets none.
    assert TicketBoltAdapter.extra_headers == {"www.ticketbolt.com": {"RSC": "1"}}
    assert "little-pika-375.convex.cloud" not in TicketBoltAdapter.extra_headers


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (
            "SHOPPING VILA VELHA, Rua Luciano das Neves, 2418 - Vila Velha/ES",
            ("SHOPPING VILA VELHA", "Rua Luciano das Neves, 2418 - Vila Velha/ES"),
        ),
        ("Praça do Papa", ("Praça do Papa", "Praça do Papa")),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_location(location: str | None, expected: tuple[str | None, str | None]) -> None:
    assert split_location(location) == expected


def test_parse_lots_keeps_active_public_lots_in_reais() -> None:
    lots: list[Any] = [
        {"isActive": True, "courtesyTicket": False, "ticketSectorName": "ARENA",
         "ticketTitle": "Inteira", "name": "4º Lote", "price": 260},
        {"isActive": True, "courtesyTicket": False, "ticketSectorName": "ARENA",
         "ticketTitle": "Meia", "name": "4º Lote", "price": 130},
        {"isActive": False, "courtesyTicket": False, "ticketSectorName": "ARENA",
         "ticketTitle": "Inteira", "name": "1º Lote", "price": 180},  # past lot
        {"isActive": True, "courtesyTicket": True, "ticketSectorName": "ARENA",
         "ticketTitle": "Cortesia", "name": "Cortesia", "price": 0},  # invitation
        {"isActive": True, "courtesyTicket": False, "ticketSectorName": "PISTA",
         "ticketTitle": "Único", "name": "Gratuito", "price": 0},  # a real free lot
        {"isActive": True, "price": 12.5},
    ]  # fmt: skip
    assert parse_lots(lots) == [
        PriceLot(name="ARENA - Inteira - 4º Lote", amount=Decimal("260.00")),
        PriceLot(name="ARENA - Meia - 4º Lote", amount=Decimal("130.00")),
        PriceLot(name="PISTA - Único - Gratuito", amount=Decimal("0.00")),
        PriceLot(name="Ingresso", amount=Decimal("12.50")),
    ]


def test_registry_keys_are_the_listing_city_names() -> None:
    assert len(for_source("ticketbolt")) == 32  # every registry row since task 8
    assert resolve("vila-velha").key("ticketbolt") == "Vila Velha"
    assert resolve("vitoria-es").key("ticketbolt") == "Vitória"
    assert (
        resolve("Rio de Janeiro").key("ticketbolt") == "Rio de Janeiro"
    )  # listed "Rio De Janeiro"


# --- record_from_page


def test_record_from_page_full() -> None:
    record = record_from_page(SOMOS_ROCK_URL, fixture("event-somos-rock.html"))
    assert record.source == "ticketbolt"
    assert record.external_id == "mx70htxeapzmtajj4ymmbqe0k98bdhds"
    assert str(record.source_url) == SOMOS_ROCK_URL
    assert record.title == "SOMOS ROCK"
    # eventDate 1793541600000 = 14:00Z on the wire, 14:00 wall time on the page: naive local.
    assert record.starts_at == datetime(2026, 11, 1, 14, 0)
    assert record.ends_at == datetime(2026, 11, 2, 5, 0)
    assert record.timezone == "America/Sao_Paulo"
    assert record.venue_name == "SHOPPING VILA VELHA"
    assert record.address == "Rua Luciano das Neves, 2418 - Vila Velha/ES, 29107-900"
    assert (record.city, record.state) == ("Vila Velha", "ES")
    assert record.latitude is None and record.longitude is None
    assert record.organizer == "Produtora Exemplo"
    assert record.category == "festas_shows"
    assert map_category("ticketbolt", record.category) == "party"
    assert record.description is not None
    assert record.description.startswith(
        "Contato EXCLUSIVO para aquisição de lounges: 00 00000-0000"
    )
    assert "1º de novembro" in record.description
    assert str(record.image_url).startswith("https://little-pika-375.convex.cloud/api/storage/")
    assert record.cancelled is False and record.closed is False
    names = [lot.name for lot in record.price_lots]
    assert "ARENA SOMOS ROCK - Inteira - 4º Lote" in names
    assert "ARENA SOMOS ROCK - Meia - 4º Lote" in names
    assert not any("Cortesia" in n for n in names)
    assert all(lot.amount > 0 for lot in record.price_lots)
    inteira = next(lot for lot in record.price_lots if lot.name.endswith("Inteira - 4º Lote"))
    assert inteira.amount == 260
    assert "price_lots_pending" not in record.raw
    event = record.raw["event"]
    assert isinstance(event, dict) and event["slug"] == "somos-rock" and event["subject"] == "music"
    for key in ("paymentMethods", "paymentAuth", "pixelTrackings", "webFeeConfigId", "userId"):
        assert key not in event
    assert "description" not in event  # the markdown is in record.description only
    assert isinstance(record.raw["lots"], list) and len(record.raw["lots"]) == 28


def test_record_from_page_without_description() -> None:
    record = record_from_page(LAGUN_URL, fixture("event-lagun-bin.html"))
    assert record.external_id == "mx757bbfe30khgwvge9ejayczh8eemwm"
    assert record.title == "LAGUN - BIN"
    assert record.starts_at == datetime(2026, 9, 25, 21, 0)  # 21:00 wall time
    assert record.description is None
    assert record.venue_name == "LAGUN CLUB LTDA"
    assert record.city == "Vitória"
    assert [(lot.name, lot.amount) for lot in record.price_lots] == [
        ("ESPAÇO LAGUN - Único - 1º Lote", Decimal("40.00")),
        ("MEZANINO - Único - 1º Lote", Decimal("70.00")),
    ]


def test_normalize_reads_wall_times_in_sao_paulo() -> None:
    record = record_from_page(SOMOS_ROCK_URL, fixture("event-somos-rock.html"))
    n = normalize(record, now=NOW)
    assert n.starts_at == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)  # 14:00 BRT
    assert n.category == "party"
    assert n.venue is not None and n.venue.postal_code == "29107-900"


def manaus_page() -> str:
    """The somos-rock page relocated to Manaus (AM), the same wall times."""
    html = fixture("event-somos-rock.html")
    assert (
        html.count('\\"state\\":\\"ES\\"') == 1 and html.count('\\"city\\":\\"Vila Velha\\"') == 1
    )
    return html.replace('\\"state\\":\\"ES\\"', '\\"state\\":\\"AM\\"').replace(
        '\\"city\\":\\"Vila Velha\\"', '\\"city\\":\\"Manaus\\"'
    )


def test_wall_time_is_read_in_the_zone_of_the_events_state() -> None:
    record = record_from_page(SOMOS_ROCK_URL, manaus_page())
    assert (record.city, record.state) == ("Manaus", "AM")
    assert record.timezone == "America/Manaus"
    assert record.starts_at == datetime(2026, 11, 1, 14, 0)  # the same naive wall time
    n = normalize(record, now=NOW)
    assert n.timezone == "America/Manaus"
    assert n.starts_at == datetime(2026, 11, 1, 18, 0, tzinfo=UTC)  # 14:00 AMT = UTC-4


def test_record_from_page_rejects_pages_without_event() -> None:
    with pytest.raises(ValueError):
        record_from_page(SOMOS_ROCK_URL, "<html><body>nothing</body></html>")


# --- discover / fetch


async def discovered(adapter: TicketBoltAdapter, city: City | None) -> list[Discovered]:
    items = [d async for d in adapter.discover(city)]
    assert all(isinstance(d, Discovered) for d in items)  # every yield carries a fingerprint
    return [d for d in items if isinstance(d, Discovered)]


async def collect(adapter: TicketBoltAdapter, city: City | None) -> list[str]:
    return [d.url for d in await discovered(adapter, city)]


async def test_discovered_ids_match_the_fetched_records() -> None:
    """ADR 0013: the crawl finds the stored listing by the id discover() carries."""
    adapter = TicketBoltAdapter(FakeFetch(routes()))
    (item,) = await discovered(adapter, resolve("vila-velha"))
    assert item.url == SOMOS_ROCK_URL and item.external_id == "mx70htxeapzmtajj4ymmbqe0k98bdhds"
    assert (await adapter.fetch(SOMOS_ROCK_URL)).external_id == item.external_id


async def test_discover_fingerprint_ignores_views_and_promotion() -> None:
    data = json.loads(fixture("query-getPublicEventsList.json"))

    async def fingerprint_of(body: dict[str, Any]) -> str:
        text = json.dumps(body)

        async def fake(source: str, url: str, *, json: object | None = None) -> str:
            return text

        (item,) = await discovered(TicketBoltAdapter(fake), resolve("vila-velha"))
        return item.fingerprint

    def somos_rock(body: dict[str, Any]) -> dict[str, Any]:
        return next(i for i in body["value"]["events"] if i["name"] == "SOMOS ROCK")

    original = await fingerprint_of(data)
    volatile = json.loads(json.dumps(data))
    somos_rock(volatile).update(visualizacoes=99999.0, featuredOnHomepageHero=True)
    assert await fingerprint_of(volatile) == original
    changed = json.loads(json.dumps(data))
    somos_rock(changed)["eventDate"] += 3600000
    assert await fingerprint_of(changed) != original


async def test_discover_filters_state_and_city() -> None:
    fake = FakeFetch(routes())
    urls = await collect(TicketBoltAdapter(fake), resolve("vila-velha"))
    assert urls == [SOMOS_ROCK_URL]
    assert len(fake.calls) == 1 and fake.calls[0][0] == QUERY_URL
    body = fake.calls[0][1]
    assert isinstance(body, dict) and "page" not in body["args"]  # first call: no page, as the site


@pytest.mark.usefixtures("greater_vitoria_only")
async def test_discover_all_of_greater_vitoria_skips_other_cities_and_states() -> None:
    fake = FakeFetch(routes())
    urls = await collect(TicketBoltAdapter(fake), None)
    assert len(urls) == 20  # 21 ES events minus Linhares; none of the 3 other-state events
    assert SOMOS_ROCK_URL in urls and LAGUN_URL in urls
    assert "https://www.ticketbolt.com/event/bbendo-por-ai-barril-tap-bar-linhares" not in urls
    assert len(set(urls)) == len(urls)
    assert all(u.startswith("https://www.ticketbolt.com/event/") for u in urls)


async def test_discover_walks_a_capital_alone_and_within_the_registry() -> None:
    data = json.loads(fixture("query-getPublicEventsList.json"))
    item = next(i for i in data["value"]["events"] if i["name"] == "LAGUN - BIN")
    item["state"], item["city"] = "AM", "MANAUS"  # matched folded, as ``Rio De Janeiro`` is
    body = json.dumps(data)

    async def fake(source: str, url: str, *, json: object | None = None) -> str:
        return body

    adapter = TicketBoltAdapter(fake)
    assert await collect(adapter, resolve("manaus")) == [LAGUN_URL]
    assert LAGUN_URL in await collect(adapter, None)  # Manaus is enabled since task 8


async def test_discover_walks_the_listing_once_per_adapter() -> None:
    fake = FakeFetch(routes())
    adapter = TicketBoltAdapter(fake)
    await collect(adapter, resolve("vila-velha"))
    await collect(adapter, resolve("vitoria"))
    assert len(fake.calls) == 1  # the national list is cached on the instance


@pytest.mark.usefixtures("greater_vitoria_only")
async def test_discover_follows_has_next_page() -> None:
    fake = FakeFetch(
        routes(),
        pages={None: "query-getPublicEventsList-p2.json", 2: "query-getPublicEventsList-last.json"},
    )
    urls = await collect(TicketBoltAdapter(fake), None)
    assert [c[1]["args"].get("page") for c in fake.calls] == [None, 2]  # type: ignore[index]
    assert len(urls) == 5  # 3 ES on the first page + 2 ES on the last


async def test_discover_stops_quietly_when_the_query_fails() -> None:
    fake = FakeFetch(routes(), pages={})
    assert await collect(TicketBoltAdapter(fake), None) == []


@pytest.mark.usefixtures("greater_vitoria_only")
async def test_discover_skips_cancelled_and_unpublished_items() -> None:
    data = json.loads(fixture("query-getPublicEventsList.json"))
    for item in data["value"]["events"]:
        if item["_id"] == "mx70htxeapzmtajj4ymmbqe0k98bdhds":
            item["is_cancelled"] = True
        elif item["name"] == "LAGUN - BIN":
            item["status"] = "Cancelado"
    body = json.dumps(data)

    async def fake(source: str, url: str, *, json: object | None = None) -> str:
        return body

    urls = await collect(TicketBoltAdapter(fake), None)
    assert SOMOS_ROCK_URL not in urls and LAGUN_URL not in urls and len(urls) == 18


async def test_fetch_returns_the_record_for_a_canonical_url() -> None:
    fake = FakeFetch(routes())
    record = await TicketBoltAdapter(fake).fetch(SOMOS_ROCK_URL)
    assert record.external_id == "mx70htxeapzmtajj4ymmbqe0k98bdhds"
    assert fake.calls == [(SOMOS_ROCK_URL, None)]  # one GET, no query
    with pytest.raises(ValueError, match="not a TicketBolt event URL"):
        await TicketBoltAdapter(fake).fetch("https://www.ticketbolt.com/evento/1")


async def test_fetch_propagates_fetch_errors() -> None:
    fake = FakeFetch({})
    with pytest.raises(FetchError):
        await TicketBoltAdapter(fake).fetch(SOMOS_ROCK_URL)
