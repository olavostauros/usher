"""CORS for the browsers the PWA runs in (spec §3, task 5).

An allowlist of origins from ``EVCRAWL_API_CORS_ORIGINS``; nothing else gets
a CORS header. A request from an allowed origin gets exactly the four
headers the site's client reads, on every response, the rate limiter's 429
included (so ``Retry-After`` is readable from the page):

    Access-Control-Allow-Origin: <that origin>   (never ``*``)
    Access-Control-Allow-Methods: GET
    Access-Control-Allow-Headers: accept
    Access-Control-Expose-Headers: retry-after

plus ``Vary: Origin`` for caches. There are no credentials. A preflight
(``OPTIONS`` with ``Access-Control-Request-Method``) from an allowed origin
is answered here with 204 and the same headers; from any other origin it
falls through to the app, which has no ``OPTIONS`` route (405).

Starlette's ``CORSMiddleware`` would add the safelisted headers to
``Allow-Headers`` and send ``Allow-Methods`` on preflights only; this one is
pure ASGI so it wraps every other middleware and its headers are the
contract, verbatim.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORS_HEADERS = (
    (b"access-control-allow-methods", b"GET"),
    (b"access-control-allow-headers", b"accept"),
    (b"access-control-expose-headers", b"retry-after"),
    (b"vary", b"Origin"),
)


class Cors:
    def __init__(self, app: ASGIApp, origins: list[str]) -> None:
        self.app = app
        self.origins = frozenset(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None or origin not in self.origins:
            await self.app(scope, receive, send)
            return
        allow = [(b"access-control-allow-origin", origin.encode("latin-1")), *CORS_HEADERS]

        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            await send({"type": "http.response.start", "status": 204, "headers": allow})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in allow:
                    response_headers.append(name.decode("latin-1"), value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_with_cors)
