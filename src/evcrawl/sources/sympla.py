"""Sympla adapter (Phase 1, Greater Vitória).

Data acquisition follows ``docs/specs/phase-1-sympla.md`` §2-§4 and ADR 0003:

- listing pages are Next.js App Router pages; the search response travels in
  React Server Components flight chunks (``self.__next_f.push``);
- event pages are Next.js Pages Router pages with a ``__NEXT_DATA__`` script;
- ticket prices come from a plain JSON endpoint keyed by event id (ADR 0004).
"""

import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError, FetchText, fetch_text
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

log = get_logger("evcrawl.sources.sympla")

LISTING_URL = "https://www.sympla.com.br/eventos/{slug}?page={page}"
TICKETS_URL = (
    "https://event-page.svc.sympla.com.br/api/event-bff/purchase/event/{id}/tickets/grouped"
)
TIMEZONE = "America/Sao_Paulo"

# Greater Vitória. ``--city`` accepts the short name or the slug; omitted means all.
CITY_SLUGS: dict[str, str] = {
    "vitoria": "vitoria-es",
    "vila-velha": "vila-velha-es",
    "serra": "serra-es",
    "cariacica": "cariacica-es",
    "viana": "viana-es",
    "guarapari": "guarapari-es",
}
MAX_LISTING_PAGES = 100  # safety cap; real listings end well before this

_FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_NEXT_DATA = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>', re.S
)
_EVENT_URL = re.compile(r"^https://www\.sympla\.com\.br/evento/[^/]+/(\d+)/?$")
_BILETO_URL = re.compile(r"^https://bileto\.sympla\.com\.br/event/(\d+)/?$")
_SEARCH_KEY = '"searchDataResult":'


def _flight_payload(html: str) -> str:
    """Concatenate the decoded RSC flight chunks of a Next.js App Router page."""
    return "".join(json.loads('"' + chunk + '"') for chunk in _FLIGHT_CHUNK.findall(html))


def _next_data(html: str) -> dict[str, Any]:
    """Return the parsed ``__NEXT_DATA__`` JSON of a Next.js Pages Router page."""
    m = _NEXT_DATA.search(html)
    if m is None:
        raise ValueError("no __NEXT_DATA__ script in page")
    data: dict[str, Any] = json.loads(m.group(1))
    return data


def _search_result(payload: str) -> dict[str, Any]:
    """Extract ``searchDataResult`` (``data``, ``total``, ``limit``, ``page``) from a payload."""
    at = payload.find(_SEARCH_KEY)
    if at < 0:
        raise ValueError("no searchDataResult in flight payload")
    result, _ = json.JSONDecoder().raw_decode(payload, at + len(_SEARCH_KEY))
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("searchDataResult has an unexpected shape")
    return result


def parse_listing(html: str) -> list[dict[str, Any]]:
    """Return the event objects of one listing page (empty on the page past the last)."""
    events: list[dict[str, Any]] = _search_result(_flight_payload(html))["data"]
    return events


def _strip_contact(listing: dict[str, Any]) -> dict[str, Any]:
    """Copy of a listing object without ``organizer.email`` (AGENTS.md §10)."""
    clean = dict(listing)
    organizer = listing.get("organizer")
    if isinstance(organizer, dict):
        clean["organizer"] = {k: v for k, v in organizer.items() if k != "email"}
    return clean


def _parse_utc(value: str) -> datetime:
    """Parse an offset-aware ISO 8601 string and convert it to UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime from source: {value!r}")
    return dt.astimezone(UTC)


def _join_address(*parts: tuple[str | None, str]) -> str | None:
    """Join non-empty address pieces: ``[(value, separator-before-value), ...]``."""
    out = ""
    for value, sep in parts:
        if value and value.strip():
            out += (sep if out else "") + value.strip()
    return out or None


def _address_from_listing(loc: dict[str, Any]) -> str | None:
    return _join_address(
        (loc.get("address"), ""),
        (loc.get("address_num"), ", "),
        (loc.get("address_alt"), " - "),
        (loc.get("neighborhood"), ", "),
        (loc.get("city"), ", "),
        (loc.get("state"), " - "),
        (loc.get("zip_code"), ", "),
    )


def _address_from_event(addr: dict[str, Any]) -> str | None:
    return _join_address(
        (addr.get("address"), ""),
        (addr.get("addressNum"), ", "),
        (addr.get("addressAlt"), " - "),
        (addr.get("neighborhood"), ", "),
        (addr.get("city"), ", "),
        (addr.get("state"), " - "),
        (addr.get("zipCode"), ", "),
    )


def _iter_tickets(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Walk ``tickets`` at the top level and inside every group and subgroup."""
    for ticket in node.get("tickets") or []:
        if isinstance(ticket, dict):
            yield ticket
    for group in (node.get("groups") or []) + (node.get("subgroups") or []):
        if isinstance(group, dict):
            yield from _iter_tickets(group)


def parse_tickets(body: str) -> list[PriceLot]:
    """Price lots from the grouped-tickets endpoint.

    ``salePriceMonetary`` is what the buyer pays (service fee included), given in
    integer cents; ``netValueMonetary`` is not reliable (seen as 0 on paid lots).
    Hidden and closed lots are skipped.
    """
    lots: list[PriceLot] = []
    for ticket in _iter_tickets(json.loads(body)):
        if not ticket.get("show", True) or ticket.get("status") == "CLOSED":
            continue
        cents = ticket.get("salePriceMonetary", {}).get("integer")
        if cents is None:
            cents = 0 if ticket.get("isFree") else None
        if cents is None:
            continue
        name = str(ticket.get("name") or "Ingresso").strip()
        lots.append(PriceLot(name=name, amount=Decimal(int(cents)) / 100))
    return lots


def _record_from_listing(url: str, listing: dict[str, Any]) -> EventRecord:
    """EventRecord for a bileto entry, built from the listing object alone (ADR 0003)."""
    loc: dict[str, Any] = listing.get("location") or {}
    organizer: dict[str, Any] = listing.get("organizer") or {}
    images: dict[str, Any] = listing.get("images") or {}
    m = _BILETO_URL.match(url)
    if m is None:
        raise ValueError(f"not a bileto event URL: {url}")
    return EventRecord(
        source=SymplaAdapter.name,
        external_id=f"bileto-{m.group(1)}",
        source_url=url,
        title=str(listing["name"]).strip(),
        starts_at=_parse_utc(listing["start_date"]),
        ends_at=_parse_utc(listing["end_date"]) if listing.get("end_date") else None,
        timezone=TIMEZONE,
        venue_name=loc.get("name") or None,
        address=_address_from_listing(loc),
        city=loc.get("city") or None,
        state=loc.get("state") or None,
        latitude=loc.get("lat"),
        longitude=loc.get("lon"),
        organizer=organizer.get("name") or None,
        image_url=images.get("original") or None,
        raw={"bileto": True, "price_lots_pending": True, "listing": _strip_contact(listing)},
    )


def _record_from_event_page(url: str, html: str, lots: list[PriceLot] | None) -> EventRecord:
    """EventRecord from an event page; ``lots`` is None when the tickets request failed."""
    event: dict[str, Any] = _next_data(html)["props"]["pageProps"]["hydrationData"][
        "eventHydration"
    ]["event"]
    addr: dict[str, Any] = event.get("eventsAddress") or {}
    geo: dict[str, Any] = addr.get("geolocation") or {}
    category: dict[str, Any] = event.get("eventsCategory") or {}
    host: dict[str, Any] = event.get("eventsHost") or {}
    images: dict[str, Any] = event.get("images") or {}
    ends = event.get("endDateMultiFormat") or {}

    free = event.get("paymentEventType") == "free"
    price_lots = list(lots or [])
    if not price_lots and free:
        price_lots = [PriceLot(name="Gratuito", amount=Decimal(0))]

    raw: dict[str, object] = {
        # ``detail`` (HTML) stays only here; ``eventsHost.description`` is free text
        # that may carry contact data and is never stored (AGENTS.md §10).
        "event": {**event, "eventsHost": {k: v for k, v in host.items() if k != "description"}},
        "tickets_fetched": lots is not None,
    }
    if lots is None and not free:
        raw["price_lots_pending"] = True
    if event.get("onlineInfo"):
        raw["online"] = True

    return EventRecord(
        source=SymplaAdapter.name,
        external_id=str(event["id"]),
        source_url=event.get("newUrl") or url,
        title=str(event["name"]).strip(),
        starts_at=_parse_utc(event["startDateMultiFormat"]["ISO8601"]),
        ends_at=_parse_utc(ends["ISO8601"]) if ends.get("ISO8601") else None,
        timezone=TIMEZONE,
        venue_name=addr.get("name") or None,
        address=_address_from_event(addr),
        city=addr.get("city") or None,
        state=addr.get("state") or None,
        latitude=geo.get("latitude"),
        longitude=geo.get("longitude"),
        organizer=host.get("name") or None,
        category=category.get("slug") or None,
        description=event.get("strippedDetail") or None,
        image_url=images.get("logoUrl") or None,
        price_lots=price_lots,
        cancelled=bool(event.get("cancelled")),
        closed=bool(event.get("isClosed")),
        raw=raw,
    )


def city_slugs(city: str | None) -> list[str]:
    """Resolve ``--city`` to Sympla listing slugs; ``None`` means all of Greater Vitória."""
    if city is None:
        return list(CITY_SLUGS.values())
    key = city.strip().lower().replace(" ", "-")
    if key in CITY_SLUGS:
        return [CITY_SLUGS[key]]
    if key in CITY_SLUGS.values():
        return [key]
    raise ValueError(f"unknown city {city!r}; known: {', '.join(CITY_SLUGS)}")


class SymplaAdapter(SourceAdapter):
    """Adapter for https://www.sympla.com.br."""

    name = "sympla"
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        self._fetch = fetch
        # bileto entries are recorded from listing data only; discover() keeps the
        # listing object so fetch(url) can build the record without a page request.
        self._bileto: dict[str, dict[str, Any]] = {}

    async def discover(self, city: str | None = None) -> AsyncIterator[str]:
        """Yield canonical event URLs for every listing page of the requested cities."""
        for slug in city_slugs(city):
            seen: set[str] = set()
            for page in range(1, MAX_LISTING_PAGES + 1):
                url = LISTING_URL.format(slug=slug, page=page)
                try:
                    html = await self._fetch(self.name, url)
                except FetchError as exc:
                    log.warning(
                        "sympla.listing_unavailable",
                        city=slug,
                        page=page,
                        url=url,
                        status=exc.status,
                    )
                    break
                try:
                    events = parse_listing(html)
                except ValueError as exc:
                    log.warning(
                        "sympla.listing_unparsable", city=slug, page=page, url=url, error=str(exc)
                    )
                    break
                log.info("sympla.listing_page", city=slug, page=page, events=len(events))
                if not events:
                    break
                for listing in events:
                    event_url = str(listing.get("url") or "")
                    if event_url in seen:
                        continue
                    if _BILETO_URL.match(event_url):
                        self._bileto[event_url] = listing
                    elif not _EVENT_URL.match(event_url):
                        log.warning("sympla.unknown_event_url", city=slug, url=event_url)
                        continue
                    seen.add(event_url)
                    yield event_url

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        if _BILETO_URL.match(url):
            listing = self._bileto.get(url)
            if listing is None:
                raise ValueError(f"bileto events are recorded from listing data only: {url}")
            return _record_from_listing(url, listing)
        m = _EVENT_URL.match(url)
        if m is None:
            raise ValueError(f"not a Sympla event URL: {url}")
        html = await self._fetch(self.name, url)
        lots: list[PriceLot] | None
        try:
            lots = parse_tickets(await self._fetch(self.name, TICKETS_URL.format(id=m.group(1))))
        except (FetchError, ValueError) as exc:
            log.warning("sympla.tickets_unavailable", url=url, error=str(exc))
            lots = None
        record = _record_from_event_page(url, html, lots)
        if record.external_id != m.group(1):
            log.warning("sympla.id_mismatch", url=url, page_id=record.external_id)
        return record
