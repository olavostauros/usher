"""Adapter contract every ticketing platform implements (AGENTS.md §5)."""

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, HttpUrl

from evcrawl.cities import City

# The HTTP layer owns the rate-limit type and the fetch callable adapters use.
from evcrawl.pipeline.fetch import FetchText, RateLimit, fetch_text

__all__ = [
    "City",
    "Discovered",
    "EventRecord",
    "PriceLot",
    "RateLimit",
    "SourceAdapter",
    "fingerprint",
]


@dataclass(frozen=True)
class Discovered:
    """One listing item as :meth:`SourceAdapter.discover` saw it (phase 3 national, ADR 0013).

    *url* is what :meth:`SourceAdapter.fetch` takes; *external_id* is the id the
    fetched record will carry, so the crawl can find the stored listing without
    fetching; *fingerprint* is :func:`fingerprint` of the listing item. The crawl
    skips the event page when the stored fingerprint matches and the event is
    not about to start.
    """

    url: str
    external_id: str
    fingerprint: str


def fingerprint(
    item: Mapping[str, object],
    *,
    drop: Iterable[str] = (),
    drop_prefixes: Iterable[str] = (),
) -> str:
    """sha256 hex of the canonical JSON of a listing item, minus its volatile keys.

    *drop* names top-level keys to leave out (view counts, scores, ordering);
    *drop_prefixes* drops every top-level key starting with one of them
    (``display*``). Nested volatile data is the adapter's job to strip before
    calling. Same canonical form as ``pipeline.upsert.payload_bytes`` (kept
    inline: the pipeline imports this package, not the other way round).
    """
    dropped = set(drop)
    prefixes = tuple(drop_prefixes)
    kept = {
        key: value
        for key, value in item.items()
        if key not in dropped and not key.startswith(prefixes)
    }
    canonical = json.dumps(kept, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PriceLot(BaseModel):
    """One price tier. Free events are ``amount == 0``, never null (§7)."""

    name: str
    amount: Decimal = Field(ge=0, decimal_places=2)
    currency: str = "BRL"


class EventRecord(BaseModel):
    """A single event as reported by one source, before normalization."""

    source: str
    external_id: str
    source_url: HttpUrl
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    timezone: str = "America/Sao_Paulo"
    venue_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    organizer: str | None = None
    category: str | None = None
    description: str | None = None
    image_url: HttpUrl | None = None
    price_lots: list[PriceLot] = Field(default_factory=list)
    cancelled: bool = False
    closed: bool = False
    raw: dict[str, object] = Field(default_factory=dict)


class SourceAdapter(ABC):
    """Discover and fetch events from one platform. Never writes to the DB."""

    name: str
    base_url: str = ""
    rate_limit: RateLimit = RateLimit()
    # Fixed headers the HTTP layer adds to this source's requests, keyed by the
    # host they apply to: ``{"api.example": {"Referer": "..."}}`` (a ``Referer``
    # an API requires, ADR 0006; ``RSC: 1`` for a Next.js site, ADR 0008). A
    # request to any other host of the source gets none of them. Never a
    # User-Agent (AGENTS.md §10; the fetch layer drops the key anyway).
    extra_headers: Mapping[str, Mapping[str, str]] = {}

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        # Every request goes through the shared HTTP layer; tests inject a fake.
        self._fetch = fetch

    @abstractmethod
    def discover(self, city: City | None = None) -> AsyncIterator[str | Discovered]:
        """Yield the event URLs of one registry city, as :class:`Discovered` or plain ``str``.

        A :class:`Discovered` lets the crawl skip the event page when the
        listing item has not changed since the last run; a plain ``str`` is
        always fetched. ``None`` means every enabled city the registry lists
        for this source (``evcrawl.cities.for_source``); a city the source has
        no key for yields nothing.
        """

    @abstractmethod
    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event as an :class:`EventRecord`."""

    @classmethod
    def refetchable(cls, url: str) -> bool:
        """Whether :meth:`fetch` can rebuild this listing from its URL alone (the hourly refresh).

        False for a listing an adapter records from discovery data only (Sympla's
        bileto events): without a fresh :meth:`discover` the fetch can only fail.
        """
        return True
