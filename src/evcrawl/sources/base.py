"""Adapter contract every ticketing platform implements (AGENTS.md §5)."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, HttpUrl

# The HTTP layer owns the rate-limit type; adapters declare one as ``rate_limit``.
from evcrawl.pipeline.fetch import RateLimit

__all__ = ["EventRecord", "PriceLot", "RateLimit", "SourceAdapter"]


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
    rate_limit: RateLimit = RateLimit()

    @abstractmethod
    def discover(self, city: str | None = None) -> AsyncIterator[str]:
        """Yield external event IDs (or URLs) for a region."""

    @abstractmethod
    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event as an :class:`EventRecord`."""
