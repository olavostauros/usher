"""Geocoding of venue addresses with a persistent cache (AGENTS.md §8, spec task 5).

Coordinates from the source win: a venue that already has them never reaches
the geocoder. Everything else is looked up in ``geocode_cache`` by
``address_normalized`` first, and only a miss calls Nominatim (URL from
settings, 1 request/second, the project User-Agent), writing the answer,
positive or negative, through to the cache.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from geoalchemy2 import WKTElement
from sqlalchemy import select
from sqlalchemy.orm import Session

from evcrawl.config import Settings, get_settings
from evcrawl.db.models import GeocodeCache
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import Fetcher, FetchError, RateLimit
from evcrawl.pipeline.normalize import NormalizedVenue

log = get_logger("evcrawl.pipeline.geocode")

SOURCE = "nominatim"  # the fetcher's rate-limit bucket for the geocoder
NOMINATIM_RATE_LIMIT = RateLimit(requests_per_second=1.0, max_retries=3)
NEGATIVE_TTL = timedelta(days=30)  # a miss is retried after this long
COUNTRY_CODES = "br"


@dataclass(frozen=True)
class GeocodeResult:
    """A cache entry: ``resolved`` false caches a miss."""

    resolved: bool
    latitude: float | None = None
    longitude: float | None = None
    created_at: datetime | None = None


class GeocodeStore(Protocol):
    """Where results are cached; the SQL store below is the real one."""

    def get(self, address_normalized: str) -> GeocodeResult | None: ...

    def put(self, address_normalized: str, result: GeocodeResult, response: object) -> None: ...


class SqlGeocodeStore:
    """``geocode_cache`` table; commits each write so a later failed upsert keeps it."""

    def __init__(self, session: Session, provider: str = SOURCE) -> None:
        self.session = session
        self.provider = provider

    def get(self, address_normalized: str) -> GeocodeResult | None:
        row = self.session.scalar(
            select(GeocodeCache).where(GeocodeCache.address_normalized == address_normalized)
        )
        if row is None:
            return None
        return GeocodeResult(row.resolved, row.latitude, row.longitude, row.created_at)

    def put(self, address_normalized: str, result: GeocodeResult, response: object) -> None:
        row = self.session.scalar(
            select(GeocodeCache).where(GeocodeCache.address_normalized == address_normalized)
        )
        if row is None:
            row = GeocodeCache(address_normalized=address_normalized)
            self.session.add(row)
        row.provider = self.provider
        row.resolved = result.resolved
        row.latitude = result.latitude
        row.longitude = result.longitude
        row.location = (
            WKTElement(f"POINT({result.longitude} {result.latitude})", srid=4326)
            if result.resolved
            else None
        )
        row.response = response if isinstance(response, dict) else {"result": response}
        row.created_at = datetime.now(UTC)
        self.session.commit()


class MemoryGeocodeStore:
    """Dict-backed store for tests and dry runs."""

    def __init__(self) -> None:
        self.entries: dict[str, GeocodeResult] = {}

    def get(self, address_normalized: str) -> GeocodeResult | None:
        return self.entries.get(address_normalized)

    def put(self, address_normalized: str, result: GeocodeResult, response: object) -> None:
        self.entries[address_normalized] = GeocodeResult(
            result.resolved, result.latitude, result.longitude, datetime.now(UTC)
        )


def search_url(base_url: str, query: str) -> str:
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": "1",
        "countrycodes": COUNTRY_CODES,
        "addressdetails": "0",
    }
    return f"{base_url.rstrip('/')}/search?{urlencode(params)}"


def parse_search(body: str) -> tuple[GeocodeResult, object]:
    """First hit of a Nominatim ``/search`` response, or a negative result."""
    results = json.loads(body)
    if not isinstance(results, list) or not results:
        return GeocodeResult(resolved=False), []
    first = results[0]
    try:
        lat, lon = float(first["lat"]), float(first["lon"])
    except (KeyError, TypeError, ValueError):
        return GeocodeResult(resolved=False), first
    keep = {k: first.get(k) for k in ("place_id", "osm_type", "osm_id", "display_name", "type")}
    return GeocodeResult(resolved=True, latitude=lat, longitude=lon), keep


def query_for(venue: NormalizedVenue) -> str:
    """Free-form query: the address, or venue name + city + state when there is none."""
    if venue.address:
        return venue.address
    return ", ".join(p for p in (venue.name, venue.city, venue.state) if p)


class Geocoder:
    """Fill missing venue coordinates from the cache, then Nominatim."""

    def __init__(
        self,
        store: GeocodeStore,
        fetcher: Fetcher,
        settings: Settings | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.settings = settings or get_settings()
        self.fetcher.rate_limits.setdefault(SOURCE, NOMINATIM_RATE_LIMIT)
        self._now = now
        self.calls = 0

    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    async def resolve(self, venue: NormalizedVenue) -> NormalizedVenue:
        """Return *venue* with coordinates when they can be found; never raises."""
        if venue.latitude is not None and venue.longitude is not None:
            return venue
        key = venue.address_normalized
        cached = self.store.get(key)
        if cached is not None and (
            cached.resolved
            or cached.created_at is None
            or self._clock() - cached.created_at < NEGATIVE_TTL
        ):
            log.debug("geocode.cache_hit", address=key, resolved=cached.resolved)
            return self._apply(venue, cached)
        result, response = await self._lookup(key, query_for(venue))
        self.store.put(key, result, response)
        return self._apply(venue, result)

    async def _lookup(self, key: str, query: str) -> tuple[GeocodeResult, object]:
        url = search_url(self.settings.nominatim_url, query)
        self.calls += 1
        try:
            # Nominatim's robots.txt disallows /search for generic agents; API use is
            # governed by its usage policy (identifying User-Agent, 1 req/s) instead.
            body = await self.fetcher.fetch_text(SOURCE, url, check_robots=False)
            result, response = parse_search(body)
        except (FetchError, ValueError) as exc:
            log.warning("geocode.failed", address=key, error=str(exc))
            return GeocodeResult(resolved=False), {"error": str(exc)}
        log.info(
            "geocode.lookup",
            address=key,
            resolved=result.resolved,
            latitude=result.latitude,
            longitude=result.longitude,
        )
        return result, response

    @staticmethod
    def _apply(venue: NormalizedVenue, result: GeocodeResult) -> NormalizedVenue:
        if not result.resolved:
            return venue
        return venue.model_copy(update={"latitude": result.latitude, "longitude": result.longitude})


__all__ = [
    "Geocoder",
    "GeocodeResult",
    "GeocodeStore",
    "MemoryGeocodeStore",
    "SqlGeocodeStore",
    "parse_search",
    "query_for",
    "search_url",
]
