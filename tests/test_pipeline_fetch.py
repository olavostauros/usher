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
    WaitingRoom,
    is_waiting_room,
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
    headers: dict[str, dict[str, dict[str, str]]] | None = None,
) -> Fetcher:
    return Fetcher(
        Settings(evcrawl_user_agent=UA),
        rate_limits=rate_limits,
        headers=headers,
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


async def test_json_body_makes_a_post_under_the_same_rules() -> None:
    clock = FakeClock()
    seen: list[httpx.Request] = []

    def body(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "success"})

    headers = {"api": {"api.example": {"Referer": "https://api.example/"}}}
    async with make_fetcher(robots_then(body), clock, headers=headers) as fetcher:
        text = await fetcher.fetch_text(
            "api", "https://api.example/query", json={"path": "events:list", "args": {"a": 1}}
        )
        await fetcher.fetch_text("api", "https://api.example/page")
    assert text == '{"status":"success"}'
    post, get = seen
    assert post.method == "POST" and post.headers["Content-Type"] == "application/json"
    assert post.content == b'{"path":"events:list","args":{"a":1}}'
    assert post.headers["User-Agent"] == UA and post.headers["Referer"] == "https://api.example/"
    assert get.method == "GET" and get.content == b""
    assert clock.sleeps == pytest.approx([1.0, 1.0])  # robots, POST, GET: one bucket


async def test_json_post_is_blocked_by_robots_and_retried_like_a_get() -> None:
    clock = FakeClock()
    statuses = iter([503, 200])

    def body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), text="ok")

    robots = "User-agent: *\nDisallow: /private/\n"
    async with make_fetcher(robots_then(body, robots), clock) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.fetch_text("s", "https://example.org/private/q", json={})
        assert await fetcher.fetch_text("s", "https://example.org/q", json={}) == "ok"
    assert BACKOFF_BASE in clock.sleeps


async def test_extra_headers_apply_to_their_source_and_host_only_and_keep_the_user_agent() -> None:
    clock = FakeClock()
    seen: list[httpx.Request] = []

    def body(request: httpx.Request) -> httpx.Response:
        seen.append(request)  # every request, robots.txt included
        return robots_then(lambda r: httpx.Response(200, text="ok"))(request)

    headers = {
        "gated": {
            "API.gated.example": {  # host keys are case-insensitive
                "Referer": "https://gated.example/",
                "User-Agent": "Mozilla/5.0 (spoof)",
            },
            "www.gated.example": {"RSC": "1"},
        },
    }
    async with make_fetcher(body, clock, headers=headers) as fetcher:
        await fetcher.fetch_text("gated", "https://api.gated.example/api/1")
        await fetcher.fetch_text("gated", "https://www.gated.example/page/1")
        await fetcher.fetch_text("gated", "https://cdn.gated.example/other/1")
        await fetcher.fetch_text("plain", "https://api.gated.example/api/1")
    assert [(r.url.host, r.url.path) for r in seen] == [
        ("api.gated.example", "/robots.txt"),
        ("api.gated.example", "/api/1"),
        ("www.gated.example", "/robots.txt"),
        ("www.gated.example", "/page/1"),
        ("cdn.gated.example", "/robots.txt"),
        ("cdn.gated.example", "/other/1"),
        ("api.gated.example", "/api/1"),  # robots.txt is cached per host
    ]
    api_robots, api, _, www, _, cdn, plain = seen
    assert api.headers["Referer"] == "https://gated.example/" and "RSC" not in api.headers
    assert api_robots.headers["Referer"] == "https://gated.example/"  # robots.txt of that host too
    assert www.headers["RSC"] == "1" and "Referer" not in www.headers
    assert "Referer" not in cdn.headers and "RSC" not in cdn.headers  # undeclared host
    assert "Referer" not in plain.headers  # another source to the same host: nothing
    # AGENTS.md §10: the project User-Agent can never be overridden per source.
    assert all(r.headers["User-Agent"] == UA for r in seen)
    assert fetcher.extra_headers("gated", "https://api.gated.example/x") == {
        "Referer": "https://gated.example/"
    }
    assert fetcher.extra_headers("gated", "https://gated.example/x") == {}


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


# --- waiting rooms


async def test_redirect_into_a_waiting_room_is_refused_not_followed() -> None:
    clock = FakeClock()
    hosts: list[str] = []
    queue = "https://ticketbolt.queue-it.net/?c=ticketbolt&e=checkoutslowqueue"

    def body(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": queue})

    async with make_fetcher(robots_then(body), clock) as fetcher:
        with pytest.raises(WaitingRoom) as info:
            await fetcher.fetch_text("s", "https://example.org/event/x")
    assert hosts == ["example.org"]  # one request, never to the queue, not retried
    assert info.value.host == "ticketbolt.queue-it.net"
    assert info.value.url == "https://example.org/event/x"
    assert "waiting room" in str(info.value)


async def test_ordinary_redirects_are_still_followed() -> None:
    clock = FakeClock()

    def body(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.org/new"})
        return httpx.Response(200, text="new")

    async with make_fetcher(robots_then(body), clock) as fetcher:
        assert await fetcher.fetch_text("s", "https://example.org/old") == "new"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("queue-it.net", True),
        ("ticketbolt.queue-it.net", True),
        ("TicketBolt.Queue-It.NET", True),
        ("notqueue-it.net", False),
        ("www.ticketbolt.com", False),
    ],
)
def test_is_waiting_room(host: str, expected: bool) -> None:
    assert is_waiting_room(host) is expected


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


async def test_check_robots_false_skips_the_robots_check() -> None:
    clock = FakeClock()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /search\n")

    async with make_fetcher(handler, clock) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.fetch_text("api", "https://example.org/search?q=x")
        assert await fetcher.fetch_text("api", "https://example.org/search?q=x", check_robots=False)
    assert paths == ["/robots.txt", "/search"]
