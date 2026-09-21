"""uTicket adapter (Phase 2, Greater Vitória).

Data acquisition follows ``docs/specs/phase-2-sources.md`` §2/§4 and ADR 0006:

- ``eventsearch`` is a public JSON API with ``city=<name>`` and ``skip``/``limit``
  pagination; its items carry no coordinates, address, category or description;
- ``eventinfo/<id>`` (place with coordinates, HTML description, schedules) and
  ``tickettype?eventId=<id>`` (price lots in cents) complete the record;
- the event page at ``uticket.com.br/event/<id>`` is a client-rendered React app
  with no data in the HTML, so it is never fetched.

Every request to ``api.uticket.com.br`` is answered 403 by Cloudflare unless it
carries ``Referer: https://uticket.com.br/``. The owner allowed that one fixed
header on 2026-09-21 (ADR 0006, addendum); it is declared in ``extra_headers``
and the fetch layer adds it to this source's requests only. The User-Agent stays
the project's descriptive one.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from selectolax.parser import HTMLParser

from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

log = get_logger("evcrawl.sources.uticket")

API = "https://api.uticket.com.br"
SEARCH_URL = API + "/eventsearch?limit={limit}&skip={skip}&city={city}&q="
EVENT_INFO_URL = API + "/eventinfo/{id}"
TICKETS_URL = API + "/tickettype?eventId={id}"
EVENT_URL = "https://uticket.com.br/event/{id}"
IMAGE_URL = "https://img.uticket.com.br/event/{id}/l"
TIMEZONE = "America/Sao_Paulo"
STATE = "ES"

# Greater Vitória. ``--city`` accepts the short key, the ``-es`` slug or the name
# the API's ``place/city`` lookup returns; omitted means all six. The API has no
# state filter, so ``discover()`` also checks the ``-ES`` suffix of ``place``.
CITY_NAMES: dict[str, str] = {
    "vitoria": "Vitória",
    "vila-velha": "Vila Velha",
    "serra": "Serra",
    "cariacica": "Cariacica",
    "viana": "Viana",
    "guarapari": "Guarapari",
}
PAGE_SIZE = 50  # the site pages by 12; 50 was accepted and keeps requests few
MAX_PAGES = 100  # safety cap; a city has a handful of pages at most

_EVENT_URL = re.compile(r"^https://uticket\.com\.br/event/([A-Za-z0-9]+)/?$")
# ``place`` (search) and ``place.address`` (eventinfo): ``<venue or street> - <City>-<UF>``
_PLACE = re.compile(r"^(?P<name>.*?)\s*-\s*(?P<city>[^-]+?)-(?P<state>[A-Z]{2})$")


def parse_search(body: str) -> tuple[list[dict[str, Any]], int]:
    """Return ``(items, count)`` of one ``eventsearch`` response."""
    data = json.loads(body)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("eventsearch response has no items list")
    return items, int(data.get("count") or 0)


def parse_place(place: str | None) -> tuple[str | None, str | None, str | None]:
    """Split ``"The Point - Vila Velha-ES"`` into ``(name, city, state)``."""
    if not place:
        return None, None, None
    m = _PLACE.match(place.strip())
    if m is None:
        return place.strip() or None, None, None
    return m["name"].strip() or None, m["city"].strip() or None, m["state"]


def _parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 string (``Z`` or offset) and convert it to UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime from source: {value!r}")
    return dt.astimezone(UTC)


def _span(event: dict[str, Any]) -> tuple[datetime, datetime | None]:
    """Earliest start and latest end over ``schedules`` (falling back to ``sessions``)."""
    slots = [s for s in (event.get("schedules") or event.get("sessions") or []) if s.get("start")]
    if not slots:
        raise ValueError("event has no schedule")
    starts = [_parse_utc(s["start"]) for s in slots]
    ends = [_parse_utc(s["end"]) for s in slots if s.get("end")]
    return min(starts), max(ends) if ends else None


def description_text(raw: str | None) -> str | None:
    """Plain text of the HTML ``description``.

    The API double-encodes it (a JSON string inside the JSON string), so a leading
    quote is decoded first. Block elements become line breaks.
    """
    if not raw:
        return None
    html = raw
    if html.startswith('"') and html.endswith('"'):
        try:
            html = json.loads(html)
        except ValueError:
            pass
    if not isinstance(html, str):
        return None
    text = HTMLParser(html).text(separator="\n")
    lines = [line.strip() for line in text.replace("\xa0", " ").splitlines()]
    return "\n".join(line for line in lines if line) or None


def parse_tickets(body: str, *, now: datetime | None = None) -> list[PriceLot]:
    """Price lots from ``tickettype``: ``price`` + ``taxPrice`` in cents, the buyer's price.

    Restricted lots (need a private link) and lots whose sale already ended are
    skipped, like Sympla's hidden and closed lots (ADR 0004).
    """
    now = now or datetime.now(UTC)
    data = json.loads(body)
    lots: list[PriceLot] = []
    for ticket in (data.get("ticketTypes") if isinstance(data, dict) else None) or []:
        if not isinstance(ticket, dict) or ticket.get("restricted"):
            continue
        sell_end = ticket.get("sellEnd")
        if sell_end and _parse_utc(sell_end) < now:
            continue
        price = ticket.get("price")
        if price is None:
            continue
        cents = int(price) + int(ticket.get("taxPrice") or 0)
        name = str(ticket.get("fullname") or ticket.get("name") or "Ingresso").strip()
        lots.append(PriceLot(name=name, amount=Decimal(cents) / 100))
    return lots


def _strip_contact(event: dict[str, Any]) -> dict[str, Any]:
    """Copy of an ``eventinfo`` object without the organizer's e-mail (AGENTS.md §10)."""
    return {k: v for k, v in event.items() if k != "userEmail"}


def record_from_event(url: str, body: str, lots: list[PriceLot] | None) -> EventRecord:
    """EventRecord from ``eventinfo``; ``lots`` is None when the tickets request failed."""
    event: dict[str, Any] = json.loads(body)
    if not isinstance(event, dict) or not event.get("id"):
        raise ValueError("eventinfo response has no event id")
    place: dict[str, Any] = event.get("place") or {}
    position: dict[str, Any] = place.get("position") or {}
    _, city, state = parse_place(place.get("address"))
    starts_at, ends_at = _span(event)

    raw: dict[str, object] = {"event": _strip_contact(event), "tickets_fetched": lots is not None}
    if lots is None:
        raw["price_lots_pending"] = True

    return EventRecord(
        source=UTicketAdapter.name,
        external_id=str(event["id"]),
        source_url=url,
        title=str(event["name"]).strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=TIMEZONE,
        venue_name=place.get("name") or None,
        address=place.get("address") or None,
        city=city,
        state=state,
        latitude=position.get("lat"),
        longitude=position.get("lng"),
        organizer=event.get("UserName") or None,
        description=description_text(event.get("description")),
        image_url=IMAGE_URL.format(id=event["id"]),
        price_lots=list(lots or []),
        raw=raw,
    )


def city_names(city: str | None) -> list[str]:
    """Resolve ``--city`` to the API's city names; ``None`` means all of Greater Vitória."""
    if city is None:
        return list(CITY_NAMES.values())
    key = city.strip().lower().replace(" ", "-").removesuffix("-es")
    if key in CITY_NAMES:
        return [CITY_NAMES[key]]
    by_name = {v.lower(): v for v in CITY_NAMES.values()}
    if city.strip().lower() in by_name:
        return [by_name[city.strip().lower()]]
    raise ValueError(f"unknown city {city!r}; known: {', '.join(CITY_NAMES)}")


class UTicketAdapter(SourceAdapter):
    """Adapter for https://uticket.com.br (JSON API at api.uticket.com.br)."""

    name = "uticket"
    base_url = "https://uticket.com.br"
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)
    # The API's WAF gates requests on this one header (ADR 0006, owner decision 2026-09-21).
    extra_headers = {"Referer": "https://uticket.com.br/"}

    async def discover(self, city: str | None = None) -> AsyncIterator[str]:
        """Yield canonical event URLs of the requested cities, one search page at a time."""
        for name in city_names(city):
            seen: set[str] = set()
            for page in range(MAX_PAGES):
                skip = page * PAGE_SIZE
                url = SEARCH_URL.format(limit=PAGE_SIZE, skip=skip, city=quote(name))
                try:
                    body = await self._fetch(self.name, url)
                except FetchError as exc:
                    log.warning(
                        "uticket.search_unavailable",
                        city=name,
                        skip=skip,
                        url=url,
                        status=exc.status,
                    )
                    break
                try:
                    items, count = parse_search(body)
                except ValueError as exc:
                    log.warning(
                        "uticket.search_unparsable", city=name, skip=skip, url=url, error=str(exc)
                    )
                    break
                log.info("uticket.search_page", city=name, skip=skip, items=len(items), count=count)
                if not items:
                    break
                for item in items:
                    event_id = str(item.get("id") or "")
                    if not event_id or event_id in seen:
                        continue
                    _, _, state = parse_place(item.get("place"))
                    if state is not None and state != STATE:
                        # ``city=`` matches by name only; another state's namesake city.
                        log.info("uticket.other_state", city=name, id=event_id, place=item["place"])
                        continue
                    seen.add(event_id)
                    yield EVENT_URL.format(id=event_id)
                if skip + len(items) >= count:
                    break

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        m = _EVENT_URL.match(url)
        if m is None:
            raise ValueError(f"not a uTicket event URL: {url}")
        event_id = m.group(1)
        body = await self._fetch(self.name, EVENT_INFO_URL.format(id=event_id))
        lots: list[PriceLot] | None
        try:
            lots = parse_tickets(await self._fetch(self.name, TICKETS_URL.format(id=event_id)))
        except (FetchError, ValueError) as exc:
            log.warning("uticket.tickets_unavailable", url=url, error=str(exc))
            lots = None
        record = record_from_event(url, body, lots)
        if record.external_id != event_id:
            log.warning("uticket.id_mismatch", url=url, api_id=record.external_id)
        return record
