"""Geocoding of venue addresses with a persistent cache (AGENTS.md §8, spec task 5).

Coordinates from the source win: a venue that already has them never reaches
the geocoder. Everything else is looked up in ``geocode_cache`` by
``address_normalized`` first, and only a miss calls Nominatim (URL from
settings, the project User-Agent, ``EVCRAWL_GEOCODE_RPS`` requests per second).
The rate may be raised against the self-hosted instance (the compose
``nominatim`` service) only: on the public instance it is clamped to its policy
of 1 request/second (:func:`public_instance`, AGENTS.md §10).

One cache miss is a **query chain** (:func:`query_chain`), each step one
Nominatim request under the same rate limit, stopping at the first hit:

1. ``address`` — the free-text address with the postal code stripped
   (punctuation and accents kept; Nominatim needs them, only the cache key is
   normalized);
2. ``street`` — street (and number) + city + state;
3. ``venue`` — venue name + city + state.

The final outcome is cached under the address key, positive or negative, and
``response.attempt`` records which step hit. A transport or HTTP failure (or an
unparsable body) on any step is **not** a miss: nothing is written to the
cache, the venue is returned without coordinates and the next run retries it
(``geocode.unavailable``); within one run the same key is not retried.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlparse

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
PUBLIC_INSTANCE = "nominatim.openstreetmap.org"
PUBLIC_RPS = 1.0  # the public instance's usage policy; never exceeded
MAX_RETRIES = 3
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
        self.responses: dict[str, object] = {}

    def get(self, address_normalized: str) -> GeocodeResult | None:
        return self.entries.get(address_normalized)

    def put(self, address_normalized: str, result: GeocodeResult, response: object) -> None:
        self.entries[address_normalized] = GeocodeResult(
            result.resolved, result.latitude, result.longitude, datetime.now(UTC)
        )
        self.responses[address_normalized] = response


def public_instance(base_url: str) -> bool:
    """Whether *base_url* is the public ``nominatim.openstreetmap.org`` (or a subdomain)."""
    host = (urlparse(base_url).hostname or "").lower()
    return host == PUBLIC_INSTANCE or host.endswith("." + PUBLIC_INSTANCE)


def rate_limit_for(settings: Settings) -> RateLimit:
    """The geocoder bucket: ``EVCRAWL_GEOCODE_RPS``, clamped to 1/s on the public instance."""
    rps = settings.evcrawl_geocode_rps
    if public_instance(settings.nominatim_url) and rps > PUBLIC_RPS:
        log.warning(
            "geocode.rate_clamped",
            nominatim_url=settings.nominatim_url,
            requested=rps,
            applied=PUBLIC_RPS,
        )
        rps = PUBLIC_RPS
    return RateLimit(requests_per_second=rps, max_retries=MAX_RETRIES)


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


# ``cep:29.108.630``, ``CEP 29050-662``, ``29050662``: the postal code and its label.
_POSTAL_CODE = re.compile(r"(?:\bcep\b\s*[:.]?\s*)?\b\d{2}\.?\d{3}[-.]?\d{3}\b", re.I)
# Runs of separators left once the postal code is gone (``Vitória - ES, `` / ``X, , Y``).
_SEPARATOR_RUN = re.compile(r"(\s*[,;\-–]\s*)(?:\s*[,;\-–]\s*)+")
_SEPARATOR = re.compile(r"\s*(?:,|;|\s[-–]\s)\s*")
_NUMBER = re.compile(r"^(?:n[ºo°.]?\s*)?(\d+[A-Za-z]?)\b", re.I)


def strip_postal_code(address: str) -> str:
    """*address* without its postal code (and a ``cep`` label), separators tidied."""
    text = _POSTAL_CODE.sub("", address)
    text = _SEPARATOR_RUN.sub(r"\1", text).strip(" ,;-–")
    return re.sub(r"\s{2,}", " ", text).strip()


def street_of(address: str) -> str | None:
    """``"Praça X, 60"`` from ``"Praça X,60 Ibes - Vila Velha"``: first part plus its number."""
    parts = [p.strip() for p in _SEPARATOR.split(address) if p.strip()]
    if not parts:
        return None
    street = parts[0]
    if len(parts) > 1:
        m = _NUMBER.match(parts[1])
        if m:
            street = f"{street}, {m.group(1)}"
    return street


def query_chain(venue: NormalizedVenue) -> list[tuple[str, str]]:
    """``(attempt, query)`` pairs to try in order; duplicates and empty steps dropped."""
    region = [p for p in (venue.city, venue.state) if p]
    steps: list[tuple[str, str]] = []
    if venue.address:
        address = strip_postal_code(venue.address)
        if address:
            steps.append(("address", address))
        street = street_of(address)
        if street and venue.city:
            steps.append(("street", ", ".join([street, *region])))
    if venue.name and venue.city:
        steps.append(("venue", ", ".join([venue.name, *region])))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for attempt, query in steps:
        if query.casefold() not in seen:
            seen.add(query.casefold())
            unique.append((attempt, query))
    return unique


def query_for(venue: NormalizedVenue) -> str:
    """First query of :func:`query_chain` (kept for callers that want one string)."""
    chain = query_chain(venue)
    return chain[0][1] if chain else venue.address_normalized


class GeocodeUnavailable(Exception):
    """Nominatim could not be asked (transport/HTTP failure or unparsable body)."""


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
        # A fetcher seeded with its own bucket for the geocoder (tests) keeps it.
        self.fetcher.rate_limits.setdefault(SOURCE, rate_limit_for(self.settings))
        self._now = now
        self.calls = 0
        self._unavailable: set[str] = set()  # keys whose lookup failed in this run

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
        if key in self._unavailable:
            return venue
        try:
            result, response = await self._lookup_chain(key, query_chain(venue))
        except GeocodeUnavailable as exc:
            # Unknown, not "not found": no cache row, so the next run retries.
            self._unavailable.add(key)
            log.warning("geocode.unavailable", address=key, error=str(exc))
            return venue
        self.store.put(key, result, response)
        return self._apply(venue, result)

    async def _lookup_chain(
        self, key: str, chain: list[tuple[str, str]]
    ) -> tuple[GeocodeResult, dict[str, object]]:
        """Try each ``(attempt, query)`` in order; the first hit wins.

        Returns the outcome to cache: on a hit, the kept Nominatim fields plus
        ``attempt``/``query``; on a miss, the list of attempts that answered
        nothing. Raises :class:`GeocodeUnavailable` when a step could not be
        asked, so that nothing is cached.
        """
        tried: list[str] = []
        for attempt, query in chain:
            result, hit = await self._lookup(key, attempt, query)
            tried.append(attempt)
            if result.resolved:
                response: dict[str, object] = {"attempt": attempt, "query": query, "tried": tried}
                if isinstance(hit, dict):
                    response.update(hit)
                return result, response
        log.info("geocode.miss", address=key, tried=tried)
        return GeocodeResult(resolved=False), {"attempt": None, "tried": tried}

    async def _lookup(self, key: str, attempt: str, query: str) -> tuple[GeocodeResult, object]:
        url = search_url(self.settings.nominatim_url, query)
        self.calls += 1
        try:
            # Nominatim's robots.txt disallows /search for generic agents; API use is
            # governed by its usage policy (identifying User-Agent, 1 req/s) instead.
            body = await self.fetcher.fetch_text(SOURCE, url, check_robots=False)
            result, response = parse_search(body)
        except (FetchError, ValueError) as exc:
            raise GeocodeUnavailable(f"{attempt}: {exc}") from exc
        log.info(
            "geocode.lookup",
            address=key,
            attempt=attempt,
            query=query,
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
    "GeocodeUnavailable",
    "MemoryGeocodeStore",
    "SqlGeocodeStore",
    "parse_search",
    "public_instance",
    "query_chain",
    "query_for",
    "rate_limit_for",
    "search_url",
    "street_of",
    "strip_postal_code",
]
