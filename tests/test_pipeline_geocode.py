"""Geocoder tests: MockTransport for Nominatim, in-memory cache; SQL cache on the scratch db."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from evcrawl.config import Settings
from evcrawl.db.models import GeocodeCache
from evcrawl.pipeline.fetch import Fetcher
from evcrawl.pipeline.geocode import (
    NEGATIVE_TTL,
    Geocoder,
    GeocodeResult,
    MemoryGeocodeStore,
    SqlGeocodeStore,
    parse_search,
    query_for,
    search_url,
)
from evcrawl.pipeline.normalize import NormalizedVenue

NOMINATIM = "https://nominatim.test"
UA = "EventoLivreBot/test (+mailto:redacted@example.org)"
HIT = [
    {
        "place_id": 1,
        "osm_type": "way",
        "osm_id": 2,
        "lat": "-20.3119792",
        "lon": "-40.3050745",
        "display_name": "Avenida Cezar Hilal, Vitória",
        "type": "residential",
        "licence": "ODbL",
    }
]


class NominatimMock:
    def __init__(self, responses: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.responses = responses or {}
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search\n")
        assert request.url.path == "/search"
        assert request.headers["User-Agent"] == UA
        q = request.url.params["q"]
        self.queries.append(q)
        return httpx.Response(200, json=self.responses.get(q, []))


async def noop_sleep(_: float) -> None:
    return None


def make_geocoder(
    mock: NominatimMock, store: MemoryGeocodeStore | None = None, now: datetime | None = None
) -> tuple[Geocoder, MemoryGeocodeStore]:
    store = store or MemoryGeocodeStore()
    fetcher = Fetcher(
        Settings(evcrawl_user_agent=UA, nominatim_url=NOMINATIM),
        transport=httpx.MockTransport(mock),
        sleep=noop_sleep,
    )
    return Geocoder(store, fetcher, Settings(nominatim_url=NOMINATIM), now=now), store


def venue(**overrides: object) -> NormalizedVenue:
    base: dict[str, object] = {
        "name": "Auditório",
        "address": "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES, 29050-662",
        "address_normalized": "avenida cezar hilal 700 bento ferreira vitoria es 29050 662",
        "city": "Vitória",
        "state": "ES",
    }
    base.update(overrides)
    return NormalizedVenue.model_validate(base)


# --- helpers


def test_search_url() -> None:
    url = search_url("https://nominatim.test/", "Rua A, 1, Vitória")
    assert url.startswith("https://nominatim.test/search?")
    assert "q=Rua+A%2C+1%2C+Vit%C3%B3ria" in url
    assert "countrycodes=br" in url and "format=jsonv2" in url and "limit=1" in url


def test_parse_search() -> None:
    result, kept = parse_search(json.dumps(HIT))
    assert result == GeocodeResult(True, -20.3119792, -40.3050745)
    assert isinstance(kept, dict) and "licence" not in kept and kept["osm_id"] == 2
    assert parse_search("[]") == (GeocodeResult(False), [])
    assert parse_search(json.dumps([{"lat": "x"}]))[0].resolved is False


def test_query_for_prefers_address() -> None:
    assert query_for(venue()).startswith("Avenida Cezar Hilal")
    assert query_for(venue(address=None)) == "Auditório, Vitória, ES"


# --- Geocoder.resolve


async def test_source_coordinates_make_zero_geocoder_calls() -> None:
    mock = NominatimMock()
    geocoder, store = make_geocoder(mock)
    v = venue(latitude=-20.3, longitude=-40.3)
    assert await geocoder.resolve(v) is v
    assert mock.queries == [] and geocoder.calls == 0 and store.entries == {}


async def test_miss_calls_nominatim_once_and_caches() -> None:
    mock = NominatimMock({venue().address: HIT})
    geocoder, store = make_geocoder(mock)
    first = await geocoder.resolve(venue())
    assert (first.latitude, first.longitude) == (-20.3119792, -40.3050745)
    second = await geocoder.resolve(venue())
    assert (second.latitude, second.longitude) == (-20.3119792, -40.3050745)
    assert mock.queries == [venue().address]  # second call served from the cache
    assert store.entries[venue().address_normalized].resolved is True


async def test_negative_result_is_cached_until_ttl_expires() -> None:
    mock = NominatimMock()  # every query answers []
    now = datetime(2026, 9, 20, tzinfo=UTC)
    geocoder, store = make_geocoder(mock, now=now)
    assert (await geocoder.resolve(venue())).latitude is None
    assert (await geocoder.resolve(venue())).latitude is None
    assert len(mock.queries) == 1
    store.entries[venue().address_normalized] = GeocodeResult(
        False, created_at=now - NEGATIVE_TTL - timedelta(days=1)
    )
    await geocoder.resolve(venue())
    assert len(mock.queries) == 2


async def test_geocoder_failure_is_a_negative_result_not_an_error() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    fetcher = Fetcher(
        Settings(evcrawl_user_agent=UA, nominatim_url=NOMINATIM),
        transport=httpx.MockTransport(broken),
        sleep=noop_sleep,
    )
    store = MemoryGeocodeStore()
    geocoder = Geocoder(store, fetcher, Settings(nominatim_url=NOMINATIM))
    resolved = await geocoder.resolve(venue())
    assert resolved.latitude is None
    assert store.entries[venue().address_normalized].resolved is False


async def test_geocoder_uses_a_one_per_second_bucket() -> None:
    geocoder, _ = make_geocoder(NominatimMock())
    assert geocoder.fetcher.rate_limit("nominatim").requests_per_second == 1.0


# --- SQL store on the scratch database


def test_sql_store_round_trip(scratch_engine: Engine) -> None:
    with Session(scratch_engine) as session:
        store = SqlGeocodeStore(session)
        key = "rua teste 1 vitoria es"
        assert store.get(key) is None
        store.put(key, GeocodeResult(True, -20.31, -40.30), {"osm_id": 1})
        got = store.get(key)
        assert (
            got is not None and got.resolved and (got.latitude, got.longitude) == (-20.31, -40.30)
        )
        store.put(key, GeocodeResult(False), [])  # overwrite, same row
        assert (
            session.scalar(
                select(GeocodeCache.resolved).where(GeocodeCache.address_normalized == key)
            )
            is False
        )
        assert session.execute(text("select count(*) from geocode_cache")).scalar_one() == 1
        store.put(key, GeocodeResult(True, -20.31, -40.30), {"osm_id": 1})
        within = session.execute(
            text(
                "select count(*) from geocode_cache where ST_DWithin(location, "
                "ST_GeogFromText('SRID=4326;POINT(-40.30 -20.31)'), 10)"
            )
        ).scalar_one()
        assert within == 1
