"""Sympla adapter (cities from the registry, ``evcrawl.cities``).

Data acquisition follows ``docs/specs/phase-1-sympla.md`` §2-§4 and ADR 0003:

- listing pages are Next.js App Router pages; the search response travels in
  React Server Components flight chunks (``self.__next_f.push``). A generic
  city page carries ``searchDataResult`` (24 per page); the curated hub page of
  the biggest capitals carries ``dataSectionMoreEvents`` instead (16 per page,
  ADR 0003 addendum). Both page with ``?page=N`` until an empty page;
- event pages are Next.js Pages Router pages with a ``__NEXT_DATA__`` script;
- ticket prices come from a plain JSON endpoint keyed by event id (ADR 0004).
"""

import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from evcrawl.cities import City, for_source, timezone_for_uf
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError, FetchText, fetch_text
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

log = get_logger("evcrawl.sources.sympla")

LISTING_URL = "https://www.sympla.com.br/eventos/{slug}?page={page}"
TICKETS_URL = (
    "https://event-page.svc.sympla.com.br/api/event-bff/purchase/event/{id}/tickets/grouped"
)
# Safety cap on the page walk; the empty page is the real stop. São Paulo's hub
# is ~240 pages of 16 (2026-09-22), the largest generic listing ~60 of 24.
MAX_LISTING_PAGES = 500

_FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_NEXT_DATA = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>', re.S
)
_EVENT_URL = re.compile(r"^https://www\.sympla\.com\.br/evento/[^/]+/(\d+)/?$")
_BILETO_URL = re.compile(r"^https://bileto\.sympla\.com\.br/event/(\d+)/?$")
# The listing block of each page template (ADR 0003 and its addendum).
_TEMPLATES: dict[str, str] = {
    "city": '"searchDataResult":',
    "hub": '"dataSectionMoreEvents":',
}


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


def _listing_block(payload: str) -> tuple[str, dict[str, Any]]:
    """``(template, block)``: the ``{data, total, limit, page}`` listing block of a payload.

    ``template`` is ``"city"`` (``searchDataResult``) or ``"hub"``
    (``dataSectionMoreEvents``); a hub page also carries curated sliders
    (``collection-matrix``), which are not read: the block's page walk covers
    the city's whole listing.
    """
    for template, key in _TEMPLATES.items():
        at = payload.find(key)
        if at < 0:
            continue
        result, _ = json.JSONDecoder().raw_decode(payload, at + len(key))
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError(f"{template} listing block has an unexpected shape")
        return template, result
    raise ValueError("no searchDataResult or dataSectionMoreEvents in flight payload")


def parse_listing(html: str) -> list[dict[str, Any]]:
    """Return the event objects of one listing page (empty on the page past the last)."""
    return parse_listing_page(html)[1]


def parse_listing_page(html: str) -> tuple[str, list[dict[str, Any]]]:
    """``(template, events)`` of one listing page; see :func:`_listing_block`."""
    template, block = _listing_block(_flight_payload(html))
    events: list[dict[str, Any]] = block["data"]
    return template, events


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
        timezone=timezone_for_uf(loc.get("state")),
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
        timezone=timezone_for_uf(addr.get("state")),
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


class SymplaAdapter(SourceAdapter):
    """Adapter for https://www.sympla.com.br."""

    name = "sympla"
    base_url = "https://www.sympla.com.br"
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        super().__init__(fetch)
        # bileto entries are recorded from listing data only; discover() keeps the
        # listing object so fetch(url) can build the record without a page request.
        self._bileto: dict[str, dict[str, Any]] = {}

    async def discover(self, city: City | None = None) -> AsyncIterator[str]:
        """Yield canonical event URLs for every listing page of the requested cities.

        The generic city listing is regional (``vitoria-es`` lists most of ES):
        every listed event is yielded, whatever its ``location.city``, as in
        Phase 1/2; the overlap between neighbouring cities is left to the
        conditional fetch (spec task 6).
        """
        for target in [city] if city is not None else for_source(self.name):
            slug = target.key(self.name)
            if slug is None:
                log.info("sympla.city_not_listed", city=target.slug)
                continue
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
                    template, events = parse_listing_page(html)
                except ValueError as exc:
                    log.warning(
                        "sympla.listing_unparsable", city=slug, page=page, url=url, error=str(exc)
                    )
                    break
                log.info(
                    "sympla.listing_page",
                    city=slug,
                    page=page,
                    template=template,
                    events=len(events),
                )
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
