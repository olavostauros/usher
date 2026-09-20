"""HTTP layer tests with ``httpx.MockTransport``; no network, no real sleeping."""

from collections.abc import Callable

import httpx
import pytest

from evcrawl.config import Settings
from evcrawl.pipeline.fetch import (
    BACKOFF_BASE,
    Fetcher,
    FetchError,
    RateLimit,
    RobotsDisallowed,
    TokenBucket,
)

UA = "EventoLivreBot/test (+mailto:redacted@example.org)"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: FakeClock,
    rate_limits: dict[str, RateLimit] | None = None,
) -> Fetcher:
    return Fetcher(
        Settings(evcrawl_user_agent=UA),
        rate_limits=rate_limits,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
        rng=lambda: 0.0,
    )


def robots_then(
    body: Callable[[httpx.Request], httpx.Response],
    robots: str | None = "User-agent: *\nAllow: /\n",
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404) if robots is None else httpx.Response(200, text=robots)
        return body(request)

    return handler


# --- token bucket


async def test_token_bucket_spaces_requests() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2.0, clock=clock, sleep=clock.sleep)
    assert await bucket.acquire() == 0.0
    assert await bucket.acquire() == pytest.approx(0.5)
    assert await bucket.acquire() == pytest.approx(0.5)
    clock.now += 10
    assert await bucket.acquire() == 0.0


# --- fetch_text happy path


async def test_fetch_text_sends_user_agent_and_returns_body() -> None:
    clock = FakeClock()
    seen: list[httpx.Request] = []

    def body(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<html>ok</html>")

    async with make_fetcher(robots_then(body), clock) as fetcher:
        assert await fetcher.fetch_text("sympla", "https://example.org/e/1") == "<html>ok</html>"
    assert [r.url.path for r in seen] == ["/e/1"]
    assert seen[0].headers["User-Agent"] == UA


async def test_rate_limit_is_per_source_and_covers_robots() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def body(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="ok")

    limits = {"slow": RateLimit(requests_per_second=0.5), "fast": RateLimit(requests_per_second=10)}
    async with make_fetcher(robots_then(body), clock, limits) as fetcher:
        await fetcher.fetch_text("slow", "https://a.example/1")  # robots + page
        await fetcher.fetch_text("slow", "https://a.example/2")
        await fetcher.fetch_text("fast", "https://b.example/1")  # robots + page, own bucket
    # slow: robots at t0, /1 at +2s, /2 at +4s; fast: robots immediately, /1 at +0.1s
    assert clock.sleeps == pytest.approx([2.0, 2.0, 0.1])
    assert calls == ["https://a.example/1", "https://a.example/2", "https://b.example/1"]


# --- retries


async def test_retries_on_5xx_with_exponential_backoff_then_succeeds() -> None:
    clock = FakeClock()
    statuses = iter([503, 500, 200])

    def body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), text="ok")

    async with make_fetcher(robots_then(body), clock) as fetcher:
        assert await fetcher.fetch_text("s", "https://example.org/x") == "ok"
    # 1s bucket wait after robots.txt, then backoff 1s and 2s (rng=0); the
    # backoff advances the clock, so the bucket has nothing left to wait for.
    assert clock.sleeps == [1.0, BACKOFF_BASE, BACKOFF_BASE * 2]


async def test_429_honors_retry_after() -> None:
    clock = FakeClock()
    statuses = iter([429, 200])

    def body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), headers={"Retry-After": "7"}, text="ok")

    async with make_fetcher(robots_then(body), clock) as fetcher:
        await fetcher.fetch_text("s", "https://example.org/x")
    assert 7.0 in clock.sleeps


async def test_gives_up_after_max_retries() -> None:
    clock = FakeClock()
    attempts = 0

    def body(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502)

    limits = {"s": RateLimit(requests_per_second=100, max_retries=2)}
    async with make_fetcher(robots_then(body), clock, limits) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch_text("s", "https://example.org/x")
    assert attempts == 3
    assert info.value.status == 502
    assert info.value.url == "https://example.org/x"


async def test_transport_errors_are_retried() -> None:
    clock = FakeClock()
    calls = 0

    def body(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text="ok")

    async with make_fetcher(robots_then(body), clock) as fetcher:
        assert await fetcher.fetch_text("s", "https://example.org/x") == "ok"
    assert calls == 2


async def test_404_is_not_retried() -> None:
    clock = FakeClock()
    calls = 0

    def body(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with make_fetcher(robots_then(body), clock) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch_text("s", "https://example.org/missing")
    assert calls == 1
    assert info.value.status == 404


# --- robots.txt


async def test_robots_disallow_blocks_without_fetching_page() -> None:
    clock = FakeClock()
    pages: list[str] = []

    def body(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.path)
        return httpx.Response(200, text="ok")

    robots = "User-agent: *\nDisallow: /private/\n"
    async with make_fetcher(robots_then(body, robots), clock) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.fetch_text("s", "https://example.org/private/1")
        assert await fetcher.fetch_text("s", "https://example.org/public/1") == "ok"
    assert pages == ["/public/1"]


async def test_robots_is_fetched_once_per_host() -> None:
    clock = FakeClock()
    robots_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            robots_hits.append(request.url.host)
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    async with make_fetcher(handler, clock) as fetcher:
        for url in ("https://a.example/1", "https://a.example/2", "https://b.example/1"):
            await fetcher.fetch_text("s", url)
    assert robots_hits == ["a.example", "b.example"]


async def test_missing_robots_allows_everything() -> None:
    clock = FakeClock()
    async with make_fetcher(
        robots_then(lambda r: httpx.Response(200, text="ok"), robots=None), clock
    ) as fetcher:
        assert await fetcher.fetch_text("s", "https://example.org/anything") == "ok"
