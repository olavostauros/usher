"""Offline parsing tests for the Ingresse adapter against sanitized fixtures."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evcrawl.cities import City, for_source, resolve
from evcrawl.pipeline.fetch import FetchError
from evcrawl.pipeline.normalize import map_category, normalize
from evcrawl.sources.base import Discovered
from evcrawl.sources.ingresse import (
    EVENT_API_URL,
    PAGE_SIZE,
    SEARCH_URL,
    IngresseAdapter,
    category_of,
    parse_search,
    record_from_event,
)

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "ingresse"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

GUARAPARI_SLUG = "turne-dominguinho-guarapari"
GUARAPARI_URL = f"https://www.ingresse.com/{GUARAPARI_SLUG}/"
SEASON_SLUG = "magia-bruxaria-outubro"
SEASON_URL = f"https://www.ingresse.com/{SEASON_SLUG}/"


def search(state: str, page: int = 0) -> str:
    return SEARCH_URL.format(state=state, size=PAGE_SIZE, offset=page * PAGE_SIZE)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetch:
    """Serves fixtures by URL and records every request; raises FetchError(404) for the rest."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, source: str, url: str) -> str:
        assert source == "ingresse"
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(url, status=404)
        return fixture(self.routes[url])


# --- fixtures hygiene (AGENTS.md §10)


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixture_has_no_email_but_the_redacted_one(path: Path) -> None:
    found = set(EMAIL.findall(path.read_text(encoding="utf-8")))
    assert found <= {"redacted@example.org"}


# --- helpers


def test_parse_search_state_es() -> None:
    items = parse_search(fixture("search-es-p1.json"))
    assert [i["slug"] for i in items][0] == GUARAPARI_SLUG
    ev = items[0]
    assert ev["id"] == 97573
    assert ev["place"]["city"] == "Guarapari" and ev["place"]["state"] == "ES"
    assert ev["place"]["location"] == {"lat": -20.75332, "lon": -40.56395}
    # search items carry no description and only the next session (ADR 0021)
    assert "description" not in ev and "sessions" not in ev


def test_parse_search_empty_and_other_shapes() -> None:
    assert parse_search(fixture("search-empty.json")) == []
    with pytest.raises(ValueError, match="not an object"):
        parse_search("[]")
    with pytest.raises(ValueError, match="no events list"):
        parse_search('{"items": []}')


def test_category_of_skips_promo_tags_and_maps() -> None:
    assert category_of([{"slug": "nao-fique-de-fora"}, {"slug": "club"}]) == "club"
    assert category_of([{"slug": "nao-fique-de-fora"}]) is None
    assert category_of([]) is None and category_of(None) is None
    for slug in ("shows", "brasilidades", "musica-eletronica", "club", "reveillon", "futebol"):
        assert map_category("ingresse", slug) != "uncategorized"


def test_registry_keys_are_the_city_names() -> None:
    assert resolve("vitoria").key("ingresse") == "Vitória"
    assert resolve("sao-paulo").key("ingresse") == "São Paulo"
    assert len(for_source("ingresse")) == 32


# --- discover()


async def discovered(adapter: IngresseAdapter, city: City | None) -> list[Discovered]:
    items = [d async for d in adapter.discover(city)]
    assert all(isinstance(d, Discovered) for d in items)
    return [d for d in items if isinstance(d, Discovered)]


async def test_discover_filters_the_state_listing_by_city() -> None:
    fake = FakeFetch({search("ES"): "search-es-p1.json"})
    adapter = IngresseAdapter(fake)
    urls = [d.url for d in await discovered(adapter, resolve("guarapari"))]
    assert urls == [GUARAPARI_URL, "https://www.ingresse.com/turne-dominguinho-guarapari-bb/"]
    assert await discovered(adapter, resolve("vitoria")) == []  # nothing in Vitória today
    assert fake.calls == [search("ES")]  # one short page, cached on the instance


async def test_discover_pages_until_a_short_page() -> None:
    # Pages 1-4 are full; the fixture of page 1 stands in for 2-4 (ids repeat, so
    # paging also stops on a page with nothing new), page 5 is short.
    fake = FakeFetch({search("SP", 0): "search-sp-p1.json", search("SP", 1): "search-sp-p5.json"})
    items = await discovered(IngresseAdapter(fake), resolve("sao-paulo"))
    assert fake.calls == [search("SP", 0), search("SP", 1)]
    page1 = parse_search(fixture("search-sp-p1.json"))
    page5 = parse_search(fixture("search-sp-p5.json"))
    expected = {str(i["id"]) for i in page1 + page5 if i["place"]["city"] == "São Paulo"}
    assert {d.external_id for d in items} == expected
    assert len(items) == len(expected)  # no duplicates
    assert SEASON_URL in {d.url for d in items}


async def test_discover_stops_on_a_page_with_nothing_new() -> None:
    fake = FakeFetch({search("SP", 0): "search-sp-p1.json", search("SP", 1): "search-sp-p1.json"})
    await discovered(IngresseAdapter(fake), resolve("sao-paulo"))
    assert fake.calls == [search("SP", 0), search("SP", 1)]


async def test_discover_treats_404_as_the_end_of_the_listing() -> None:
    # A state without events, and the page past the last one, answer 404.
    fake = FakeFetch({search("SP", 0): "search-sp-p1.json"})
    items = await discovered(IngresseAdapter(fake), resolve("sao-paulo"))
    assert items and fake.calls == [search("SP", 0), search("SP", 1)]
    assert await discovered(IngresseAdapter(FakeFetch({})), resolve("manaus")) == []


async def test_discover_logs_and_skips_an_unavailable_listing() -> None:
    async def broken(source: str, url: str) -> str:
        raise FetchError(url, status=503)

    assert await discovered(IngresseAdapter(broken), resolve("vitoria")) == []

    async def garbage(source: str, url: str) -> str:
        return "[]"

    assert await discovered(IngresseAdapter(garbage), resolve("vitoria")) == []


async def test_discovered_ids_match_the_fetched_records() -> None:
    """ADR 0013: the crawl finds the stored listing by the id discover() carries."""
    fake = FakeFetch(
        {
            search("ES"): "search-es-p1.json",
            EVENT_API_URL.format(slug=GUARAPARI_SLUG): f"event-{GUARAPARI_SLUG}.json",
        }
    )
    adapter = IngresseAdapter(fake)
    items = await discovered(adapter, resolve("guarapari"))
    item = next(d for d in items if d.url == GUARAPARI_URL)
    assert (await adapter.fetch(item.url)).external_id == item.external_id == "97573"


async def test_discover_fingerprint_ignores_score_and_tracking() -> None:
    page = json.loads(fixture("search-es-p1.json"))

    async def fingerprint_of(body: dict[str, object]) -> str:
        async def fake(source: str, url: str) -> str:
            return json.dumps(body)

        items = await discovered(IngresseAdapter(fake), resolve("guarapari"))
        return next(d.fingerprint for d in items if d.url == GUARAPARI_URL)

    original = await fingerprint_of(page)
    volatile = json.loads(json.dumps(page))
    volatile["events"][0].update(delta=0.3, trackingTags={"facebook": {"id": "1"}})
    assert await fingerprint_of(volatile) == original
    changed = json.loads(json.dumps(page))
    changed["events"][0]["title"] = "Outro título"
    assert await fingerprint_of(changed) != original


# --- fetch()


async def test_fetch_parses_a_single_session_event() -> None:
    fake = FakeFetch({EVENT_API_URL.format(slug=GUARAPARI_SLUG): f"event-{GUARAPARI_SLUG}.json"})
    rec = await IngresseAdapter(fake).fetch(GUARAPARI_URL)
    assert fake.calls == [EVENT_API_URL.format(slug=GUARAPARI_SLUG)]

    assert rec.source == "ingresse"
    assert rec.external_id == "97573"
    assert str(rec.source_url) == GUARAPARI_URL
    assert rec.title == "Turnê Dominguinho - Guarapari"
    assert rec.starts_at == datetime(2027, 1, 1, 21, 0, tzinfo=UTC)
    assert rec.ends_at is None  # one session: open-ended
    assert rec.timezone == "America/Sao_Paulo"
    assert rec.venue_name == "P12 Guarapari"
    assert rec.address == "Rodovia do Sol, Km26 - Guarapari-ES - 29208-720"
    assert rec.city == "Guarapari" and rec.state == "ES"
    assert (rec.latitude, rec.longitude) == (-20.75332, -40.56395)
    assert rec.category == "brasilidades"
    assert rec.description is not None
    assert rec.description.startswith("Depois de conquistar o Brasil")
    assert "<p>" not in rec.description and "\n" in rec.description
    assert str(rec.image_url).startswith("https://kraken.ingresse.com/event/posters/97573/large/")
    assert rec.price_lots == []
    assert rec.raw["price_lots_pending"] is True
    event = rec.raw["event"]
    assert isinstance(event, dict)
    assert "eventDisclaimer" not in event and "trackingTags" not in event


async def test_fetch_spans_every_session() -> None:
    fake = FakeFetch({EVENT_API_URL.format(slug=SEASON_SLUG): f"event-{SEASON_SLUG}.json"})
    rec = await IngresseAdapter(fake).fetch(SEASON_URL)
    assert rec.starts_at == datetime(2025, 10, 22, 23, 0, tzinfo=UTC)  # the first of 37
    assert rec.ends_at == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # the last one's start
    assert rec.category is None  # no categories on this event
    assert rec.city == "São Paulo" and rec.state == "SP"


def test_normalize_keeps_the_record_and_marks_prices_pending() -> None:
    rec = record_from_event(GUARAPARI_URL, fixture(f"event-{GUARAPARI_SLUG}.json"))
    n = normalize(rec, now=datetime(2026, 10, 9, tzinfo=UTC))
    assert n.price_lots_pending is True
    assert n.category == "music"


def test_record_falls_back_to_the_uf_zone_and_tolerates_no_location() -> None:
    event = json.loads(fixture(f"event-{GUARAPARI_SLUG}.json"))
    event["timezone"] = "Not/A_Zone"
    event["place"] = {"city": "Manaus", "state": "AM", "name": "Teatro"}
    rec = record_from_event("https://www.ingresse.com/x/", json.dumps(event))
    assert rec.timezone == "America/Manaus"
    assert rec.latitude is None and rec.longitude is None
    assert rec.address == "Manaus-AM"


def test_record_rejects_a_response_without_id_or_session() -> None:
    with pytest.raises(ValueError, match="no id"):
        record_from_event(GUARAPARI_URL, '{"detail": "No event found"}')
    event = json.loads(fixture(f"event-{GUARAPARI_SLUG}.json"))
    event["sessions"] = []
    with pytest.raises(ValueError, match="no session"):
        record_from_event(GUARAPARI_URL, json.dumps(event))


async def test_fetch_rejects_foreign_urls() -> None:
    adapter = IngresseAdapter(FakeFetch({}))
    for url in ("https://www.sympla.com.br/evento/x/1", "https://www.ingresse.com/a/b/"):
        with pytest.raises(ValueError, match="not an Ingresse event URL"):
            await adapter.fetch(url)
