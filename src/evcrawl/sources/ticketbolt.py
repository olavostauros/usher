"""TicketBolt adapter (cities from the registry, ``evcrawl.cities``).

Data acquisition follows ``docs/specs/phase-2-sources.md`` §2/§4 and ADR 0008:

- the site is a Next.js app whose listing is rendered on the client from one
  Convex query, ``events:getPublicEventsList``, which answers a plain JSON
  ``POST`` without any token; the state picker is a client-side filter, so
  ``discover()`` takes the national list once per adapter instance and keeps
  the requested cities by ``state`` and ``city`` (spec §4, state-level sources);
- the event page ``/event/<slug>`` is server-rendered: one RSC flight row
  carries the event, its price lots (reais) and a markdown description. Since
  the owner's decision of 2026-09-21 (ADR 0008, addendum) ``fetch()`` asks for
  the flight stream itself with ``RSC: 1`` (declared in ``extra_headers`` for
  ``www.ticketbolt.com`` only); the parser reads whichever arrived, the raw
  stream or an HTML page whose ``self.__next_f.push`` chunks carry it;
- ``eventDate`` is the local wall time serialized as if it were UTC; the record
  keeps it naive with the zone of the event's UF, and ``normalize`` converts.
"""

import json
import re
import unicodedata
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from evcrawl.cities import City, fold, for_source, timezone_for_uf
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError, FetchText, fetch_text
from evcrawl.sources.base import (
    Discovered,
    EventRecord,
    PriceLot,
    RateLimit,
    SourceAdapter,
    fingerprint,
)

log = get_logger("evcrawl.sources.ticketbolt")

SITE = "https://www.ticketbolt.com"
QUERY_URL = "https://little-pika-375.convex.cloud/api/query"
LISTING_QUERY = "events:getPublicEventsList"
EVENT_URL = SITE + "/event/{slug}"
COUNTRY = "BRA"
PUBLISHED = "Publicado"
# Query-item keys left out of the fingerprint (ADR 0013): the view counter, the
# homepage promotion flag and the creation stamp (constant, carries nothing).
VOLATILE_LISTING_KEYS = ("visualizacoes", "featuredOnHomepageHero", "_creationTime")
CLOSED_STATUSES = frozenset({"Cancelado", "Finalizado"})
MAX_PAGES = 50  # safety cap on ``hasNextPage``

_EVENT_URL = re.compile(r"^https://www\.ticketbolt\.com/event/([a-z0-9-]+)/?$")
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_ROW = re.compile(rb"([0-9a-f]*):")
_WS = re.compile(r"\s+")
# Fields of the event object that are payment/tracking configuration, not event data.
_DROP = frozenset({"paymentMethods", "paymentAuth", "pixelTrackings", "webFeeConfigId", "userId"})


def event_slug(name: str) -> str:
    """The site's ``generateEventSlug``: ``"SOMOS ROCK 2027"`` -> ``somos-rock-2027``."""
    text = unicodedata.normalize("NFD", name.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s-]", "", text, flags=re.ASCII).strip()
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", text)).strip("-")


def listing_args(page: int | None = None, *, now: datetime | None = None) -> dict[str, object]:
    """Arguments of the listing query: what the site sends, plus ``page`` when following on."""
    now = now or datetime.now(UTC)
    args: dict[str, object] = {"country": COUNTRY, "now": int(now.timestamp() * 1000)}
    if page is not None:
        args["page"] = page
    return args


def query_body(path: str, args: dict[str, object]) -> dict[str, object]:
    """Convex HTTP API body for ``POST /api/query``."""
    return {"path": path, "args": args, "format": "json"}


def parse_listing(body: str) -> tuple[list[dict[str, Any]], bool]:
    """``(events, has_next_page)`` of one listing query response."""
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("status") != "success":
        message = data.get("errorMessage") if isinstance(data, dict) else None
        raise ValueError(f"listing query failed: {message or 'no success status'}")
    value = data.get("value") or {}
    events = value.get("events")
    if not isinstance(events, list):
        raise ValueError("listing query response has no events list")
    pagination = value.get("pagination") or {}
    return [e for e in events if isinstance(e, dict)], bool(pagination.get("hasNextPage"))


def is_html(body: str) -> bool:
    """An HTML document rather than a raw RSC flight stream (which starts with ``<id>:``)."""
    return body.lstrip()[:1] == "<"


def flight_stream(body: str) -> bytes:
    """The RSC flight stream of a response.

    With ``RSC: 1`` the server answers the stream itself (``text/x-component``);
    without it, or when the header is ignored, the HTML page carries the same
    stream split over ``self.__next_f.push([1, "..."])`` chunks.
    """
    if is_html(body):
        return "".join(json.loads(f'"{c}"') for c in _PUSH.findall(body)).encode("utf-8")
    return body.encode("utf-8")


def flight_rows(body: str) -> dict[str, str]:
    """RSC flight rows of a Next.js page or payload, by row id.

    The stream is ``<id>:<json>\\n`` rows; ``<id>:T<hex length>,<text>`` rows
    carry raw text (the description) of exactly that many bytes.
    """
    data = flight_stream(body)
    rows: dict[str, str] = {}
    pos = 0
    while pos < len(data):
        m = _ROW.match(data, pos)
        if m is None:
            break
        row_id, pos = m.group(1).decode(), m.end()
        if data[pos : pos + 1] == b"T":
            comma = data.index(b",", pos)
            length = int(data[pos + 1 : comma], 16)
            pos = comma + 1
            rows[row_id] = data[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
        else:
            end = data.find(b"\n", pos)
            end = len(data) if end < 0 else end
            rows[row_id] = data[pos:end].decode("utf-8", errors="replace")
            pos = end + 1
    return rows


def _find_detail(node: Any) -> dict[str, Any] | None:
    """Depth-first search of a decoded RSC row for the ``initialEventDetail`` prop."""
    if isinstance(node, dict):
        detail = node.get("initialEventDetail")
        if isinstance(detail, dict):
            return detail
        for value in node.values():
            found = _find_detail(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_detail(value)
            if found is not None:
                return found
    return None


def parse_event_page(body: str) -> tuple[dict[str, Any], dict[str, str]]:
    """``(initialEventDetail, rows)`` of an event page or payload; raises when it has none."""
    rows = flight_rows(body)
    for text in rows.values():
        if "initialEventDetail" not in text:
            continue
        try:
            node = json.loads(text)
        except ValueError:
            continue
        detail = _find_detail(node)
        if detail is not None:
            return detail, rows
    raise ValueError("event page carries no initialEventDetail")


def resolve_text(value: Any, rows: dict[str, str]) -> str | None:
    """A string prop, or the text row a ``"$<id>"`` reference points to."""
    if not isinstance(value, str):
        return None
    if value.startswith("$"):
        value = rows.get(value[1:], "")
    lines = [_WS.sub(" ", line).strip() for line in value.replace("\xa0", " ").splitlines()]
    return "\n".join(line for line in lines if line) or None


def _wall_time(ms: Any) -> datetime | None:
    """Naive local wall time from an epoch-ms serialized as if UTC (ADR 0008).

    The zone is the event's UF's (``record.timezone``); ``normalize.to_utc``
    reads the naive value in it.
    """
    try:
        seconds = float(ms) / 1000
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def parse_lots(lots: list[Any]) -> list[PriceLot]:
    """Price lots from ``datesListing.lotes``: active, public, priced in reais.

    Courtesy lots are invitations, not prices, and are skipped even when active.
    The name is ``<sector> - <ticket title> - <lot>`` so ``Inteira``/``Meia`` stay distinct.
    """
    out: list[PriceLot] = []
    for lot in lots:
        if not isinstance(lot, dict) or not lot.get("isActive") or lot.get("courtesyTicket"):
            continue
        title = str(lot.get("ticketTitle") or "").strip()
        if title.lower() == "cortesia" or lot.get("price") is None:
            continue
        parts = [
            _WS.sub(" ", str(lot.get(k) or "")).strip()
            for k in ("ticketSectorName", "ticketTitle", "name")
        ]
        name = " - ".join(p for p in parts if p) or "Ingresso"
        try:
            amount = _money(lot["price"])
        except (ArithmeticError, ValueError):
            continue
        out.append(PriceLot(name=name, amount=amount))
    return out


def split_location(location: str | None) -> tuple[str | None, str | None]:
    """``"SHOPPING VILA VELHA, Rua Luciano das Neves, 2418 - Vila Velha/ES"`` -> venue, address."""
    text = _WS.sub(" ", location or "").strip()
    if not text:
        return None, None
    venue, sep, rest = text.partition(",")
    if not sep or not rest.strip():
        return text, text
    return venue.strip() or None, rest.strip()


def record_from_page(url: str, body: str) -> EventRecord:
    """EventRecord from the ``initialEventDetail`` of an event page or its RSC payload."""
    detail, rows = parse_event_page(body)
    event: dict[str, Any] = detail.get("event") or {}
    if not event.get("_id"):
        raise ValueError("event page has no event id")
    listing: dict[str, Any] = detail.get("datesListing") or {}
    lots_raw = listing.get("lotes") or []
    images: dict[str, Any] = detail.get("imageUrls") or {}

    starts_at = _wall_time(event.get("eventDate"))
    if starts_at is None:
        raise ValueError("event has no eventDate")
    ends_at = _wall_time(event.get("eventEndDateTime"))
    venue_name, address = split_location(event.get("location"))
    zip_code = str(event.get("zipCode") or "").strip()
    if address and zip_code and zip_code not in address:
        address = f"{address}, {zip_code}"
    lots = parse_lots(lots_raw)
    status = str(event.get("status") or "")

    raw: dict[str, object] = {
        "event": {k: v for k, v in event.items() if k not in _DROP and k != "description"},
        "lots": lots_raw,
        "event_dates": listing.get("eventDates") or [],
    }
    if not lots:
        raw["price_lots_pending"] = True
    if event.get("type") == "online":
        raw["online"] = True

    return EventRecord(
        source=TicketBoltAdapter.name,
        external_id=str(event["_id"]),
        source_url=url,
        title=_WS.sub(" ", str(event.get("name") or "")).strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=timezone_for_uf(event.get("state")),
        venue_name=venue_name,
        address=address,
        city=event.get("city") or None,
        state=event.get("state") or None,
        organizer=_WS.sub(" ", str(event.get("producerName") or "")).strip() or None,
        category=event.get("categoryKey") or None,
        description=resolve_text(event.get("description"), rows),
        image_url=images.get("main") or images.get("banner") or None,
        price_lots=lots,
        cancelled=bool(event.get("is_cancelled")),
        closed=status in CLOSED_STATUSES,
        raw=raw,
    )


class TicketBoltAdapter(SourceAdapter):
    """Adapter for https://www.ticketbolt.com (Convex query + server-rendered event pages)."""

    name = "ticketbolt"
    base_url = SITE
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)
    # Ask the event pages for the RSC flight stream instead of the rendered
    # page (ADR 0008 addendum, owner decision 2026-09-21); the Convex host
    # gets nothing extra.
    extra_headers = {"www.ticketbolt.com": {"RSC": "1"}}

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        super().__init__(fetch)
        self._items: list[dict[str, Any]] | None = None  # the national list, once per instance

    async def _listing(self) -> list[dict[str, Any]]:
        """Walk the national listing query once; later calls reuse it."""
        if self._items is not None:
            return self._items
        items: list[dict[str, Any]] = []
        page: int | None = None
        for _ in range(MAX_PAGES):
            body = query_body(LISTING_QUERY, listing_args(page))
            page_items, has_next = parse_listing(await self._fetch(self.name, QUERY_URL, json=body))
            log.info("ticketbolt.listing", page=page, events=len(page_items), has_next=has_next)
            items.extend(page_items)
            if not has_next:
                break
            page = (page or 1) + 1
        self._items = items
        return items

    async def discover(self, city: City | None = None) -> AsyncIterator[str | Discovered]:
        """Yield canonical event URLs of the requested cities from the national listing."""
        wanted: set[tuple[str, str]] = set()  # (UF, folded city)
        for target in [city] if city is not None else for_source(self.name):
            name = target.key(self.name)
            if name is None:
                log.info("ticketbolt.city_not_listed", city=target.slug)
                continue
            wanted.add((target.uf, fold(name)))
        try:
            items = await self._listing()
        except FetchError as exc:
            log.warning("ticketbolt.listing_unavailable", status=exc.status)
            return
        except ValueError as exc:
            log.warning("ticketbolt.listing_unparsable", error=str(exc))
            return
        seen: set[str] = set()
        for item in items:
            event_id = str(item.get("_id") or "")
            if not event_id or event_id in seen:
                continue
            if (str(item.get("state") or ""), fold(str(item.get("city") or ""))) not in wanted:
                continue
            if item.get("is_cancelled") or item.get("status") not in (None, PUBLISHED):
                log.info("ticketbolt.skipped", id=event_id, status=item.get("status"))
                continue
            slug = event_slug(str(item.get("name") or ""))
            if not slug:
                continue
            seen.add(event_id)
            yield Discovered(
                EVENT_URL.format(slug=slug),
                event_id,
                fingerprint(item, drop=VOLATILE_LISTING_KEYS),
            )

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        m = _EVENT_URL.match(url)
        if m is None:
            raise ValueError(f"not a TicketBolt event URL: {url}")
        url = EVENT_URL.format(slug=m.group(1))
        body = await self._fetch(self.name, url)
        log.info(
            "ticketbolt.event_payload",
            url=url,
            kind="html" if is_html(body) else "rsc",
            bytes=len(body.encode("utf-8")),
        )
        return record_from_page(url, body)
