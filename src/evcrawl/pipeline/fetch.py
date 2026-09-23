"""Crawling with rate limits and retries (AGENTS.md §6 step 4, spec task 3).

One :class:`Fetcher` per CLI invocation, or one per scheduler process shared
by every job, owns one ``httpx.AsyncClient`` with the User-Agent from
settings. Every request goes through a per-source token bucket honoring the
adapter's :class:`RateLimit`, carries the adapter's fixed extra
headers for the request's host only (``extra_headers``, e.g. a ``Referer`` an
API insists on, ADR 0006, or ``RSC: 1`` for a Next.js site, ADR 0008; never a
User-Agent, AGENTS.md §10), is retried with exponential backoff and jitter on
429/5xx and transport errors, and is checked against the host's ``robots.txt``
(cached per host). Adapters only ever call ``await fetch_text(source, url)``.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from evcrawl.config import Settings, get_settings
from evcrawl.logging import get_logger

log = get_logger("evcrawl.pipeline.fetch")

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE = 1.0  # seconds; attempt n waits base * 2**n plus jitter
BACKOFF_MAX = 60.0
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class RateLimit:
    """Per-source politeness settings, declared on each adapter (AGENTS.md §6 step 4)."""

    requests_per_second: float = 1.0
    max_retries: int = 5


class FetchError(Exception):
    """An HTTP request failed after retries. ``status`` is the last response code, if any."""

    def __init__(self, url: str, status: int | None = None, message: str | None = None) -> None:
        self.url = url
        self.status = status
        super().__init__(message or f"fetch failed for {url} (status={status})")


class RobotsDisallowed(FetchError):
    """``robots.txt`` forbids this URL for our User-Agent (AGENTS.md §6 step 4)."""

    def __init__(self, url: str) -> None:
        super().__init__(url, None, f"disallowed by robots.txt: {url}")


class FetchText(Protocol):
    """Callable adapters use for every request: ``await fetch_text(source, url)``.

    ``json=`` turns the request into a ``POST`` with that JSON body (APIs that
    only answer POST, ADR 0008); everything else is a ``GET``.
    """

    async def __call__(self, source: str, url: str, *, json: object | None = None) -> str: ...


class TokenBucket:
    """Spaces requests at ``requests_per_second`` per source; async-safe."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Block until a request may go; return how long it waited."""
        async with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.interval
        if wait > 0:
            await self._sleep(wait)
        return wait


class Fetcher:
    """One HTTP client per invocation or process: rate limits, retries, robots.txt, logs.

    Safe to share between concurrent runs in one event loop: buckets and the
    ``robots.txt`` cache are created without awaiting and guarded by locks.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        rate_limits: dict[str, RateLimit] | None = None,
        headers: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.settings = settings or get_settings()
        self.rate_limits = dict(rate_limits or {})
        # source -> host -> fixed extra headers, applied only to that source's
        # requests to that host (including its robots.txt); the User-Agent is
        # never overridable here (AGENTS.md §10).
        self.headers: dict[str, dict[str, dict[str, str]]] = {
            source: {
                host.lower(): {k: v for k, v in hdrs.items() if k.lower() != "user-agent"}
                for host, hdrs in by_host.items()
            }
            for source, by_host in (headers or {}).items()
        }
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._buckets: dict[str, TokenBucket] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.settings.evcrawl_user_agent},
            follow_redirects=True,
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- policy lookups

    def rate_limit(self, source: str) -> RateLimit:
        return self.rate_limits.get(source, RateLimit())

    def extra_headers(self, source: str, url: str) -> dict[str, str]:
        """The fixed headers *source* declared for *url*'s host; empty for any other host."""
        host = (urlsplit(url).hostname or "").lower()
        return self.headers.get(source, {}).get(host, {})

    def _bucket(self, source: str) -> TokenBucket:
        if source not in self._buckets:
            self._buckets[source] = TokenBucket(
                self.rate_limit(source).requests_per_second, clock=self._clock, sleep=self._sleep
            )
        return self._buckets[source]

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), BACKOFF_MAX)
        return min(BACKOFF_BASE * 2.0**attempt, BACKOFF_MAX) + self._rng()

    # --- robots.txt, cached per host

    async def allowed(self, source: str, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        lock = self._robots_locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host not in self._robots:
                self._robots[host] = await self._load_robots(source, host)
        parser = self._robots[host]
        if parser is None:
            return True
        return parser.can_fetch(self.settings.evcrawl_user_agent, url)

    async def _load_robots(self, source: str, host: str) -> RobotFileParser | None:
        """Fetch and parse ``robots.txt``; ``None`` (allow all) when it is missing or broken."""
        url = urlunsplit((*urlsplit(host)[:2], "/robots.txt", "", ""))
        try:
            response = await self._request(source, url)
        except FetchError as exc:
            log.info("fetch.robots_unavailable", source=source, url=url, status=exc.status)
            return None
        if response.status_code != 200:
            return None
        parser = RobotFileParser(url)
        parser.parse(response.text.splitlines())
        log.info("fetch.robots_loaded", source=source, url=url)
        return parser

    # --- requests

    async def fetch_text(
        self,
        source: str,
        url: str,
        *,
        json: object | None = None,
        check_robots: bool = True,
    ) -> str:
        """GET *url* under *source*'s rate limit and return the body as text.

        With ``json`` the request is a ``POST`` carrying that JSON body, under
        the same bucket, retries, headers and ``robots.txt`` check.
        ``check_robots=False`` is for APIs we are permitted to call by their
        usage policy (Nominatim) whose ``robots.txt`` still says "no crawlers";
        crawled sites always keep the check.
        """
        if check_robots and not await self.allowed(source, url):
            log.warning("fetch.robots_disallowed", source=source, url=url)
            raise RobotsDisallowed(url)
        response = await self._request(source, url, json=json)
        if response.status_code != 200:
            raise FetchError(url, response.status_code)
        return response.text

    async def _request(
        self, source: str, url: str, *, json: object | None = None
    ) -> httpx.Response:
        """One rate-limited GET (POST with *json*), retried; returns any non-retryable response."""
        max_retries = self.rate_limit(source).max_retries
        headers = self.extra_headers(source, url)
        method = "GET" if json is None else "POST"
        last_status: int | None = None
        for attempt in range(max_retries + 1):
            waited = await self._bucket(source).acquire()
            response: httpx.Response | None = None
            try:
                response = await self._client.request(method, url, headers=headers, json=json)
            except httpx.TransportError as exc:
                log.warning(
                    "fetch.transport_error",
                    source=source,
                    url=url,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                last_status = None
            else:
                last_status = response.status_code
                log.info(
                    "fetch.response",
                    source=source,
                    method=method,
                    url=url,
                    status=response.status_code,
                    attempt=attempt,
                    waited=round(waited, 3),
                    bytes=len(response.content),
                )
                if response.status_code not in RETRY_STATUSES:
                    return response
            if attempt < max_retries:
                delay = self._backoff(attempt, response)
                log.info(
                    "fetch.retry", source=source, url=url, attempt=attempt, delay=round(delay, 2)
                )
                await self._sleep(delay)
        log.error(
            "fetch.gave_up", source=source, url=url, status=last_status, attempts=max_retries + 1
        )
        raise FetchError(url, last_status)


@lru_cache(maxsize=1)
def get_fetcher() -> Fetcher:
    """Process-wide fetcher for callers that did not build their own (default rate limits)."""
    return Fetcher()


async def fetch_text(source: str, url: str, *, json: object | None = None) -> str:
    """Module-level convenience: :meth:`Fetcher.fetch_text` on the process-wide fetcher."""
    return await get_fetcher().fetch_text(source, url, json=json)


__all__ = [
    "FetchError",
    "FetchText",
    "Fetcher",
    "RateLimit",
    "RobotsDisallowed",
    "TokenBucket",
    "fetch_text",
]
