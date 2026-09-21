"""Geocoder tests: MockTransport for Nominatim, in-memory cache; SQL cache on the scratch db.

The query-chain tests replay responses recorded on 2026-09-21 from the public
Nominatim with the project User-Agent (``tests/fixtures/http/nominatim/``) for
the BlueTicket venue Clube ARCI, whose free-text address defeated the single
raw query.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from evcrawl.config import Settings
from evcrawl.db.models import GeocodeCache
from evcrawl.pipeline.fetch import Fetcher, RateLimit
from evcrawl.pipeline.geocode import (
    NEGATIVE_TTL,
    Geocoder,
    GeocodeResult,
    MemoryGeocodeStore,
    SqlGeocodeStore,
    parse_search,
    query_chain,
    query_for,
    search_url,
    street_of,
    strip_postal_code,
)
from evcrawl.pipeline.normalize import NormalizedVenue

NOMINATIM = "https://nominatim.test"
UA = "EventoLivreBot/test (+mailto:redacted@example.org)"
FIXTURES = Path(__file__).parent / "fixtures" / "http" / "nominatim"

# Clube ARCI (BlueTicket, Vila Velha): the address as the organizer typed it.
ARCI_ADDRESS = "Praça Assis chateaubriand,60 Ibes - Vila Velha - cep:29.108.630"
ARCI_KEY = "praca assis chateaubriand 60 ibes vila velha cep 29 108 630"
ARCI_QUERIES = {
    "address": "Praça Assis chateaubriand,60 Ibes - Vila Velha",
    "street": "Praça Assis chateaubriand, 60, Vila Velha, ES",
    "venue": "Clube ARCI, Vila Velha, ES",
}


def fixture(name: str) -> list[dict[str, object]]:
    data = json.loads((FIXTURES / f"search-clube-arci-{name}.json").read_text())
    assert isinstance(data, list)
    return data


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


def arci() -> NormalizedVenue:
    return venue(
        name="Clube ARCI", address=ARCI_ADDRESS, address_normalized=ARCI_KEY, city="Vila Velha"
    )


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


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (ARCI_ADDRESS, "Praça Assis chateaubriand,60 Ibes - Vila Velha"),
        (
            "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES, 29050-662",
            "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES",
        ),
        ("CEP 29050-662, Rua X, 1", "Rua X, 1"),
        ("Rua Y 10 29102920", "Rua Y 10"),
        ("Rua Z, 1, Centro", "Rua Z, 1, Centro"),  # nothing to strip, untouched
    ],
)
def test_strip_postal_code_keeps_punctuation_and_accents(address: str, expected: str) -> None:
    assert strip_postal_code(address) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Praça Assis chateaubriand,60 Ibes - Vila Velha", "Praça Assis chateaubriand, 60"),
        ("Avenida Cezar Hilal, 700, Bento Ferreira", "Avenida Cezar Hilal, 700"),
        ("Rua Sete-de-Setembro, nº 12 - Centro", "Rua Sete-de-Setembro, 12"),
        ("Rua Sem Número - Centro", "Rua Sem Número"),
        ("", None),
    ],
)
def test_street_of(address: str, expected: str | None) -> None:
    assert street_of(address) == expected


def test_query_chain_for_a_free_text_address() -> None:
    assert query_chain(arci()) == list(ARCI_QUERIES.items())


def test_query_chain_drops_empty_and_duplicate_steps() -> None:
    assert query_chain(venue(address=None)) == [("venue", "Auditório, Vitória, ES")]
    assert query_chain(venue(address=None, name=None)) == []
    # A bare street name as the address: the "street" step is the same text plus
    # the region, the "venue" step would repeat it.
    v = venue(
        name="Praça Assis chateaubriand", address="Praça Assis chateaubriand", city="Vila Velha"
    )
    assert query_chain(v) == [
        ("address", "Praça Assis chateaubriand"),
        ("street", "Praça Assis chateaubriand, Vila Velha, ES"),
    ]
    # No city: the region steps are skipped, the address alone is tried.
    assert query_chain(venue(city=None, state=None)) == [
        ("address", "Avenida Cezar Hilal, 700, Bento Ferreira, Vitória - ES")
    ]


# --- Geocoder.resolve


async def test_source_coordinates_make_zero_geocoder_calls() -> None:
    mock = NominatimMock()
    geocoder, store = make_geocoder(mock)
    v = venue(latitude=-20.3, longitude=-40.3)
    assert await geocoder.resolve(v) is v
    assert mock.queries == [] and geocoder.calls == 0 and store.entries == {}


async def test_miss_calls_nominatim_once_and_caches() -> None:
    first_query = query_for(venue())  # the address minus its postal code
    mock = NominatimMock({first_query: HIT})
    geocoder, store = make_geocoder(mock)
    first = await geocoder.resolve(venue())
    assert (first.latitude, first.longitude) == (-20.3119792, -40.3050745)
    second = await geocoder.resolve(venue())
    assert (second.latitude, second.longitude) == (-20.3119792, -40.3050745)
    assert mock.queries == [first_query]  # second call served from the cache
    assert store.entries[venue().address_normalized].resolved is True


async def test_negative_result_is_cached_until_ttl_expires() -> None:
    mock = NominatimMock()  # every query answers []
    now = datetime(2026, 9, 20, tzinfo=UTC)
    geocoder, store = make_geocoder(mock, now=now)
    assert (await geocoder.resolve(venue())).latitude is None
    assert (await geocoder.resolve(venue())).latitude is None
    chain = len(query_chain(venue()))
    assert len(mock.queries) == chain == 3  # one full chain, then the cached miss
    store.entries[venue().address_normalized] = GeocodeResult(
        False, created_at=now - NEGATIVE_TTL - timedelta(days=1)
    )
    await geocoder.resolve(venue())
    assert len(mock.queries) == 2 * chain


async def test_negative_result_records_every_attempt() -> None:
    mock = NominatimMock()  # every query answers []
    geocoder, store = make_geocoder(mock)
    assert (await geocoder.resolve(arci())).latitude is None
    assert mock.queries == list(ARCI_QUERIES.values())  # the whole chain, in order
    assert store.entries[ARCI_KEY].resolved is False
    assert store.responses[ARCI_KEY] == {
        "attempt": None,
        "tried": ["address", "street", "venue"],
    }


# --- the query chain, replaying the recorded Clube ARCI responses


async def test_chain_hits_on_the_address_without_postal_code() -> None:
    # Recorded 2026-09-21: the raw address answers [], the same text minus the
    # cep answers the street; the later steps are never asked.
    mock = NominatimMock(
        {
            ARCI_ADDRESS: fixture("raw-address-empty"),
            ARCI_QUERIES["address"]: fixture("address-no-cep"),
            ARCI_QUERIES["street"]: fixture("street-number"),
            ARCI_QUERIES["venue"]: fixture("venue"),
        }
    )
    geocoder, store = make_geocoder(mock)
    resolved = await geocoder.resolve(arci())
    assert (resolved.latitude, resolved.longitude) == (-20.3508099, -40.3138558)
    assert mock.queries == [ARCI_QUERIES["address"]] and geocoder.calls == 1
    assert store.entries[ARCI_KEY].resolved is True
    response = store.responses[ARCI_KEY]
    assert response["attempt"] == "address" and response["tried"] == ["address"]
    assert response["query"] == ARCI_QUERIES["address"]
    assert response["osm_id"] == 361993575 and "licence" not in response


async def test_chain_falls_back_to_street_then_venue() -> None:
    mock = NominatimMock({ARCI_QUERIES["venue"]: fixture("venue")})
    geocoder, store = make_geocoder(mock)
    resolved = await geocoder.resolve(arci())
    assert (resolved.latitude, resolved.longitude) == (-20.3499417, -40.3144514)  # the club
    assert mock.queries == list(ARCI_QUERIES.values())
    response = store.responses[ARCI_KEY]
    assert response["attempt"] == "venue"
    assert response["tried"] == ["address", "street", "venue"]
    assert response["display_name"].startswith("Clube Arci, Praça Assis Chateaubriand")

    mock = NominatimMock({ARCI_QUERIES["street"]: fixture("street-number")})
    geocoder, store = make_geocoder(mock)
    assert (await geocoder.resolve(arci())).latitude == -20.3508099
    assert mock.queries == [ARCI_QUERIES["address"], ARCI_QUERIES["street"]]
    assert store.responses[ARCI_KEY]["attempt"] == "street"


async def test_chain_hit_is_served_from_the_cache_afterwards() -> None:
    mock = NominatimMock({ARCI_QUERIES["venue"]: fixture("venue")})
    geocoder, _ = make_geocoder(mock)
    await geocoder.resolve(arci())
    await geocoder.resolve(arci())
    assert geocoder.calls == 3  # one chain, not two


# --- failures are "unknown, retry next run", never a cached miss


def broken_geocoder(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[Geocoder, MemoryGeocodeStore]:
    fetcher = Fetcher(
        Settings(evcrawl_user_agent=UA, nominatim_url=NOMINATIM),
        rate_limits={"nominatim": RateLimit(requests_per_second=100, max_retries=0)},
        transport=httpx.MockTransport(handler),
        sleep=noop_sleep,
    )
    store = MemoryGeocodeStore()
    return Geocoder(store, fetcher, Settings(nominatim_url=NOMINATIM)), store


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_http_failure_is_not_cached(status: int) -> None:
    calls = 0

    def broken(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="Access denied")

    geocoder, store = broken_geocoder(broken)
    resolved = await geocoder.resolve(arci())
    assert resolved.latitude is None
    assert store.entries == {}  # no resolved=false row
    assert calls == 1  # the chain stops at the first failed step
    # The same key is not retried within the run, only by the next run.
    await geocoder.resolve(arci())
    assert calls == 1 and geocoder.calls == 1


async def test_transport_failure_and_bad_body_are_not_cached() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    geocoder, store = broken_geocoder(down)
    assert (await geocoder.resolve(venue())).latitude is None
    assert store.entries == {}

    geocoder, store = broken_geocoder(lambda r: httpx.Response(200, text="<html>not json"))
    assert (await geocoder.resolve(venue())).latitude is None
    assert store.entries == {}


async def test_failure_after_a_miss_is_still_not_cached() -> None:
    # Step 1 answers [] (a real miss), step 2 fails: the outcome is unknown.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == ARCI_QUERIES["address"]:
            return httpx.Response(200, json=[])
        return httpx.Response(502)

    geocoder, store = broken_geocoder(handler)
    assert (await geocoder.resolve(arci())).latitude is None
    assert store.entries == {}


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
