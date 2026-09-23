"""Adapter contract every ticketing platform implements (AGENTS.md §5)."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, HttpUrl

from evcrawl.cities import City

# The HTTP layer owns the rate-limit type and the fetch callable adapters use.
from evcrawl.pipeline.fetch import FetchText, RateLimit, fetch_text

__all__ = ["City", "EventRecord", "PriceLot", "RateLimit", "SourceAdapter"]


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
    def discover(self, city: City | None = None) -> AsyncIterator[str]:
        """Yield external event IDs (or URLs) of one registry city.

        ``None`` means every enabled city the registry lists for this source
        (``evcrawl.cities.for_source``); a city the source has no key for
        yields nothing.
        """

    @abstractmethod
    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event as an :class:`EventRecord`."""
