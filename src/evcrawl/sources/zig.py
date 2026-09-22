"""Zig adapter (cities from the registry, ``evcrawl.cities``).

Data acquisition follows ``docs/specs/phase-2-sources.md`` §2/§4 and ADR 0009:

- the listing is a public static JSON feed (``new-events.json`` on S3, the same
  object the home page embeds): national, so ``discover()`` keeps the requested
  cities by ``event_location.state``/``city`` (spec §4);
- the event page ``/eventos/<slug>`` is server-rendered: ``__NEXT_DATA__``
  carries the event (HTML description, location, producer, JSON-LD) and the
  tickets on sale (``value`` + ``fee`` in reais);
- the page has no category, so ``fetch()`` reads it from the feed item, loaded
  once per adapter instance;
- the feed's ``organization`` block carries phone and e-mail: stripped here.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from selectolax.parser import HTMLParser

from evcrawl.cities import DEFAULT_TIMEZONE, City, fold, for_source, timezone_for_uf
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError, FetchText, fetch_text
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

log = get_logger("evcrawl.sources.zig")

SITE = "https://zig.tickets"
FEED_URL = "https://zigtickets-static-zig-tickets.s3.us-east-1.amazonaws.com/domains/zig.tickets/new-events.json"
EVENT_URL = SITE + "/eventos/{slug}"

_EVENT_URL = re.compile(r"^https://zig\.tickets/eventos/([A-Za-z0-9_-]+)/?$")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_WS = re.compile(r"\s+")
# Feed ``organization`` keys that are not contact data (AGENTS.md §10).
_ORGANIZATION_KEEP = frozenset({"id", "name", "slug"})
# Page ``event`` keys that are tracking or checkout configuration, or kept elsewhere.
_EVENT_DROP_PREFIXES = ("fb_", "g_", "seats_integration")
_EVENT_DROP = frozenset(
    {"description", "json_ld", "sponsors", "gateway", "default_acquirer", "discount"}
)


def category_slug(name: str | None) -> str | None:
    """``"Shows e Festivais"`` -> ``"shows-e-festivais"`` (the key ``normalize.py`` maps)."""
    if not name:
        return None
    return re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-") or None


def parse_feed(body: str) -> list[dict[str, Any]]:
    """Return the event objects of the ``new-events.json`` feed."""
    data = json.loads(body)
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise ValueError("feed has no events list")
    return [item for item in events if isinstance(item, dict)]


def parse_next_data(html: str) -> dict[str, Any]:
    """``pageProps`` of a server-rendered page's ``__NEXT_DATA__``."""
    m = _NEXT_DATA.search(html)
    if m is None:
        raise ValueError("page has no __NEXT_DATA__")
    data = json.loads(m.group(1))
    props = (data.get("props") or {}).get("pageProps") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        raise ValueError("__NEXT_DATA__ has no pageProps")
    return props


def _parse_iso(value: Any) -> datetime | None:
    """Aware datetime from the site's ``2026-11-07T15:00:00.000-03:00`` strings."""
    if not isinstance(value, str) or not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))


def _timezone(value: Any, state: Any) -> str:
    """The event's zone when the image's tzdata knows it, else its UF's (ADR 0009)."""
    if isinstance(value, str) and value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            log.info("zig.unknown_timezone", timezone=value, state=state)
        else:
            return value
    return timezone_for_uf(state if isinstance(state, str) else None)


def _text(html: str | None) -> str | None:
    """Plain text of an HTML fragment; block elements become line breaks."""
    if not html:
        return None
    text = HTMLParser(html).text(separator="\n").replace("\xa0", " ")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line) or None


def _money(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


def parse_tickets(tickets: dict[str, Any] | None) -> list[PriceLot]:
    """Price lots from ``pageProps.tickets.availables``: ``value`` + ``fee``, the buyer's price.

    Only public tickets count; the name already carries the sector and the lot
    (``Pista [Meia] 3º Lote``), the sector is prefixed when it does not.
    """
    lots: list[PriceLot] = []
    for ticket in (tickets or {}).get("availables") or []:
        if not isinstance(ticket, dict) or not ticket.get("public", 1):
            continue
        if ticket.get("value") is None:
            continue
        name = _WS.sub(" ", str(ticket.get("name") or "")).strip() or "Ingresso"
        sector = _WS.sub(" ", str(ticket.get("sector_name") or "")).strip()
        if sector and sector.lower() not in name.lower():
            name = f"{sector} - {name}"
        try:
            amount = _money(ticket["value"]) + _money(ticket.get("fee"))
        except (ArithmeticError, ValueError):
            continue
        lots.append(PriceLot(name=name, amount=amount))
    return lots


def strip_contact(item: dict[str, Any]) -> dict[str, Any]:
    """Copy of a feed item with ``organization`` reduced to id, name and slug (AGENTS.md §10)."""
    clean = dict(item)
    organization = item.get("organization")
    if isinstance(organization, dict):
        clean["organization"] = {k: v for k, v in organization.items() if k in _ORGANIZATION_KEEP}
    return clean


def _clean_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in event.items()
        if k not in _EVENT_DROP and not k.startswith(_EVENT_DROP_PREFIXES)
    }


def record_from_page(url: str, html: str, listing: dict[str, Any] | None = None) -> EventRecord:
    """EventRecord from an event page; *listing* is the feed item (category), when known."""
    props = parse_next_data(html)
    event: dict[str, Any] = props.get("event") or {}
    if not event.get("id"):
        raise ValueError("event page has no event id")
    location: dict[str, Any] = event.get("event_location") or {}
    producer: dict[str, Any] = event.get("producer") or {}
    json_ld: dict[str, Any] = event.get("json_ld") or {}
    tickets: dict[str, Any] = props.get("tickets") or {}

    starts_at = _parse_iso(event.get("start_date"))
    if starts_at is None:
        raise ValueError("event has no start_date")
    latitude: float | None = None
    longitude: float | None = None
    if location.get("lat") and location.get("lng"):
        latitude, longitude = float(location["lat"]), float(location["lng"])
    categories = (listing or {}).get("event_categories") or []
    first = next((c for c in categories if isinstance(c, dict) and c.get("name")), None)
    lots = parse_tickets(tickets)

    raw: dict[str, object] = {
        "event": _clean_event(event),
        "tickets": tickets.get("availables") or [],
        "listing": strip_contact(listing) if listing else None,
    }
    if not lots:
        raw["price_lots_pending"] = True
    if event.get("is_online"):
        raw["online"] = True

    return EventRecord(
        source=ZigAdapter.name,
        external_id=str(event["id"]),
        source_url=url,
        title=_WS.sub(" ", str(event.get("name") or "")).strip(),
        starts_at=starts_at,
        ends_at=_parse_iso(event.get("end_date")),
        timezone=_timezone(event.get("timezone"), location.get("state")),
        venue_name=_WS.sub(" ", str(location.get("name") or "")).strip() or None,
        address=_WS.sub(" ", str(location.get("formatted_address") or "")).strip() or None,
        city=location.get("city") or None,
        state=location.get("state") or None,
        latitude=latitude,
        longitude=longitude,
        organizer=_WS.sub(" ", str(producer.get("name") or "")).strip() or None,
        category=category_slug(first["name"]) if first else None,
        description=_text(event.get("description")),
        image_url=json_ld.get("image") or event.get("banner") or None,
        price_lots=lots,
        cancelled=str(json_ld.get("eventStatus") or "").endswith("EventCancelled"),
        closed=bool(event.get("is_closed")),
        raw=raw,
    )


class ZigAdapter(SourceAdapter):
    """Adapter for https://zig.tickets (static JSON feed + server-rendered event pages)."""

    name = "zig"
    base_url = SITE
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        super().__init__(fetch)
        self._feed: dict[str, dict[str, Any]] | None = None  # slug -> feed item, once per run

    async def _load_feed(self) -> dict[str, dict[str, Any]]:
        if self._feed is None:
            items = parse_feed(await self._fetch(self.name, FEED_URL))
            self._feed = {str(i.get("slug") or ""): i for i in items if i.get("slug")}
            log.info("zig.feed", events=len(self._feed))
        return self._feed

    async def discover(self, city: City | None = None) -> AsyncIterator[str]:
        """Yield canonical event URLs of the requested cities from the national feed."""
        wanted: set[tuple[str, str]] = set()  # (UF, folded city)
        for target in [city] if city is not None else for_source(self.name):
            name = target.key(self.name)
            if name is None:
                log.info("zig.city_not_listed", city=target.slug)
                continue
            wanted.add((target.uf, fold(name)))
        try:
            feed = await self._load_feed()
        except FetchError as exc:
            log.warning("zig.feed_unavailable", url=FEED_URL, status=exc.status)
            return
        except ValueError as exc:
            log.warning("zig.feed_unparsable", url=FEED_URL, error=str(exc))
            return
        now = datetime.now(UTC)
        seen: set[str] = set()
        for slug, item in feed.items():
            event_id = str(item.get("id") or "")
            location = item.get("event_location") or {}
            if not event_id or event_id in seen:
                continue
            state = str(location.get("state") or "").strip().upper()
            if (state, fold(str(location.get("city") or ""))) not in wanted:
                continue
            if item.get("is_closed"):
                continue
            last = _parse_iso(item.get("end_date")) or _parse_iso(item.get("start_date"))
            if last is not None and last < now:
                continue
            seen.add(event_id)
            yield EVENT_URL.format(slug=slug)

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        m = _EVENT_URL.match(url)
        if m is None:
            raise ValueError(f"not a Zig event URL: {url}")
        slug = m.group(1)
        listing: dict[str, Any] | None = None
        try:
            listing = (await self._load_feed()).get(slug)
        except (FetchError, ValueError) as exc:
            log.warning("zig.feed_unavailable", url=FEED_URL, error=str(exc))
        html = await self._fetch(self.name, EVENT_URL.format(slug=slug))
        record = record_from_page(EVENT_URL.format(slug=slug), html, listing)
        if listing is None:
            log.info("zig.no_listing_item", url=url)
        return record
