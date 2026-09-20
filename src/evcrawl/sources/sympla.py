"""Sympla adapter (Phase 1). Discovery and parsing are not implemented yet."""

from collections.abc import AsyncIterator

from evcrawl.sources.base import EventRecord, RateLimit, SourceAdapter


class SymplaAdapter(SourceAdapter):
    """Adapter for https://www.sympla.com.br."""

    name = "sympla"
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    async def discover(self, city: str | None = None) -> AsyncIterator[str]:
        raise NotImplementedError("sympla discovery is not implemented yet")
        yield ""  # pragma: no cover - makes this an async generator

    async def fetch(self, external_id: str) -> EventRecord:
        raise NotImplementedError("sympla fetch is not implemented yet")
