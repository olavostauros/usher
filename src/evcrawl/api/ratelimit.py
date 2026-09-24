"""A per-client token bucket in process (spec §3 "Abuse limits").

60 requests a minute per client on ``/v1/*``, refilled one a second. The
client is the socket peer, or the first ``X-Forwarded-For`` address only when
``EVCRAWL_API_TRUST_PROXY`` is on (behind our own proxy; otherwise the header
is the client's to forge). Addresses live in this dict only, for the window,
and are never logged (LGPD). There is no Redis: one process, one bucket set;
limits at the edge are part of exposure (task 5).
"""

import time
from collections.abc import Callable

from starlette.requests import Request

PER_MINUTE = 60
_PRUNE_EVERY = 1000  # requests between sweeps of idle clients


class RateLimiter:
    def __init__(self, per_minute: int = PER_MINUTE, clock: Callable[[], float] = time.monotonic):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # tokens per second
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens, at)
        self._since_prune = 0

    def take(self, client: str) -> float:
        """Spend one token for ``client``: ``0`` when allowed, else seconds until one refills."""
        now = self.clock()
        tokens, at = self._buckets.get(client, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - at) * self.rate)
        if tokens >= 1:
            self._buckets[client] = (tokens - 1, now)
            self._prune(now)
            return 0.0
        self._buckets[client] = (tokens, now)
        return (1 - tokens) / self.rate

    def _prune(self, now: float) -> None:
        self._since_prune += 1
        if self._since_prune < _PRUNE_EVERY:
            return
        self._since_prune = 0
        idle = self.capacity / self.rate  # a full refill: nothing to remember before it
        self._buckets = {c: b for c, b in self._buckets.items() if now - b[1] < idle}


def client_key(request: Request, *, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
