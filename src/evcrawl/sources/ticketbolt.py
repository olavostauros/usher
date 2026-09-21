"""TicketBolt adapter (Phase 2, Greater Vitória).

Data acquisition follows ``docs/specs/phase-2-sources.md`` §2/§4 and ADR 0008:

- the site is a Next.js app whose listing is rendered on the client from one
  Convex query, ``events:getPublicEventsList``, which answers a plain JSON
  ``POST`` without any token; the state picker is a client-side filter, so
  ``discover()`` takes the national list once and keeps ``state == "ES"`` and
  the requested cities (spec §4, state-level sources);
- the event page ``/event/<slug>`` is server-rendered: one RSC flight row
  carries the event, its price lots (reais) and a markdown description;
- ``eventDate`` is the local wall time serialized as if it were UTC.
"""

import json
import re
import unicodedata
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

log = get_logger("evcrawl.sources.ticketbolt")

SITE = "https://www.ticketbolt.com"
QUERY_URL = "https://little-pika-375.convex.cloud/api/query"
LISTING_QUERY = "events:getPublicEventsList"
EVENT_URL = SITE + "/event/{slug}"
TIMEZONE = "America/Sao_Paulo"
COUNTRY = "BRA"
STATE = "ES"
PUBLISHED = "Publicado"
CLOSED_STATUSES = frozenset({"Cancelado", "Finalizado"})
MAX_PAGES = 50  # safety cap on ``hasNextPage``

# Greater Vitória. ``--city`` accepts the short key, the ``-es`` slug or the
# listing's ``city``; omitted means all six. The listing is national, so the
# filter is applied to ``state``/``city`` after discovery (spec §4).
CITY_NAMES: dict[str, str] = {
    "vitoria": "Vitória",
    "vila-velha": "Vila Velha",
    "serra": "Serra",
    "cariacica": "Cariacica",
    "viana": "Viana",
    "guarapari": "Guarapari",
}

_EVENT_URL = re.compile(r"^https://www\.ticketbolt\.com/event/([a-z0-9-]+)/?$")
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_ROW = re.compile(rb"([0-9a-f]*):")
_WS = re.compile(r"\s+")
# Fields of the event object that are payment/tracking configuration, not event data.
_DROP = frozenset({"paymentMethods", "paymentAuth", "pixelTrackings", "webFeeConfigId", "userId"})


def _key(text: str) -> str:
    """Lowercase, accent-insensitive comparison key for city names."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


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


def flight_rows(html: str) -> dict[str, str]:
    """RSC flight rows of a Next.js page, by row id.

    The ``self.__next_f.push([1, "..."])`` chunks form one stream of
    ``<id>:<json>\\n`` rows; ``<id>:T<hex length>,<text>`` rows carry raw text
    (the description) of exactly that many bytes.
    """
    data = "".join(json.loads(f'"{c}"') for c in _PUSH.findall(html)).encode("utf-8")
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


def parse_event_page(html: str) -> tuple[dict[str, Any], dict[str, str]]:
    """``(initialEventDetail, rows)`` of an event page; raises when the page has none."""
    rows = flight_rows(html)
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
    """Naive America/Sao_Paulo wall time from an epoch-ms serialized as if UTC (ADR 0008)."""
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


def record_from_page(url: str, html: str) -> EventRecord:
    """EventRecord from an event page's ``initialEventDetail``."""
    detail, rows = parse_event_page(html)
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
        timezone=TIMEZONE,
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


def city_names(city: str | None) -> list[str]:
    """Resolve ``--city`` to the listing's ``city`` values; ``None`` means all six cities."""
    if city is None:
        return list(CITY_NAMES.values())
    key = _key(city).replace(" ", "-").removesuffix("-es")
    if key in CITY_NAMES:
        return [CITY_NAMES[key]]
    raise ValueError(f"unknown city {city!r}; known: {', '.join(CITY_NAMES)}")


class TicketBoltAdapter(SourceAdapter):
    """Adapter for https://www.ticketbolt.com (Convex query + server-rendered event pages)."""

    name = "ticketbolt"
    base_url = SITE
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    async def discover(self, city: str | None = None) -> AsyncIterator[str]:
        """Yield canonical event URLs of the requested cities from the national listing."""
        wanted = {_key(name) for name in city_names(city)}
        seen: set[str] = set()
        page: int | None = None
        for _ in range(MAX_PAGES):
            body = query_body(LISTING_QUERY, listing_args(page))
            try:
                items, has_next = parse_listing(await self._fetch(self.name, QUERY_URL, json=body))
            except FetchError as exc:
                log.warning("ticketbolt.listing_unavailable", page=page, status=exc.status)
                return
            except ValueError as exc:
                log.warning("ticketbolt.listing_unparsable", page=page, error=str(exc))
                return
            log.info("ticketbolt.listing", page=page, events=len(items), has_next=has_next)
            for item in items:
                event_id = str(item.get("_id") or "")
                if not event_id or event_id in seen:
                    continue
                if item.get("state") != STATE or _key(str(item.get("city") or "")) not in wanted:
                    continue
                if item.get("is_cancelled") or item.get("status") not in (None, PUBLISHED):
                    log.info("ticketbolt.skipped", id=event_id, status=item.get("status"))
                    continue
                slug = event_slug(str(item.get("name") or ""))
                if not slug:
                    continue
                seen.add(event_id)
                yield EVENT_URL.format(slug=slug)
            if not has_next:
                return
            page = (page or 1) + 1

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        m = _EVENT_URL.match(url)
        if m is None:
            raise ValueError(f"not a TicketBolt event URL: {url}")
        html = await self._fetch(self.name, EVENT_URL.format(slug=m.group(1)))
        return record_from_page(EVENT_URL.format(slug=m.group(1)), html)
