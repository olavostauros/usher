"""Crawling with rate limits and retries.

Only the interface adapters code against exists so far; the shared ``httpx``
client, token bucket and retries are task 3 of ``docs/specs/phase-1-sympla.md``.
"""

from typing import Protocol


class FetchError(Exception):
    """An HTTP request failed after retries. ``status`` is the last response code, if any."""

    def __init__(self, url: str, status: int | None = None, message: str | None = None) -> None:
        self.url = url
        self.status = status
        super().__init__(message or f"fetch failed for {url} (status={status})")


class FetchText(Protocol):
    """Callable adapters use for every request: ``await fetch_text(source, url)``."""

    async def __call__(self, source: str, url: str) -> str: ...


async def fetch_text(source: str, url: str) -> str:
    """Return the response body of a GET on *url*, under *source*'s rate limit."""
    raise NotImplementedError("the HTTP layer is task 3 of docs/specs/phase-1-sympla.md")


__all__ = ["FetchError", "FetchText", "fetch_text"]
