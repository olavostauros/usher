"""Ingresse adapter (cities from the registry, ``evcrawl.cities``).

Data acquisition follows ``docs/SPECIFICATION.md`` §2 and ADR 0021:

- the site is a client-rendered Next.js app; the public JSON API behind it,
  ``api-site.ingresse.com``, answers a plain GET with no key, cookie or header;
- ``events/search?iso_code=BRA-<UF>`` lists a whole state, paged by item
  ``offset`` in pages of ``size``; ``pagination.total_pages`` is wrong, so paging
  stops at a short page, and a state without events (or a page past the end)
  answers 404 or 200 with ``{"detail": "No events found"}``; ``discover()``
  filters by ``place.city``;
- ``events/<slug>`` adds the HTML description, every session and the venue with
  coordinates and the IANA zone;
- price lots are not collected: the tickets endpoint needs an ``apikey`` and the
  owner chose on 2026-10-09 not to use the one in Ingresse's site code, so every
  record is ``price_lots_pending``.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from selectolax.parser import HTMLParser

from evcrawl.cities import City, fold, for_source, timezone_for_uf
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError, FetchText, fetch_text
from evcrawl.sources.base import (
    Discovered,
    EventRecord,
    RateLimit,
    SourceAdapter,
    fingerprint,
)

log = get_logger("evcrawl.sources.ingresse")

API = "https://api-site.ingresse.com"
SEARCH_URL = (
    API + "/events/search?company_id=1&iso_code=BRA-{state}&size={size}&offset={offset}"
    "&order_by_date=true"
)
EVENT_API_URL = API + "/events/{slug}"
EVENT_URL = "https://www.ingresse.com/{slug}/"
PAGE_SIZE = 40  # what the site asks for
MAX_PAGES = 50  # safety cap; São Paulo had 5 pages on 2026-10-09
# Listing-item keys left out of the fingerprint (ADR 0013): the search score and
# the organizer's ad-tracking ids.
VOLATILE_LISTING_KEYS = ("delta", "trackingTags")
# Detail keys never kept in ``raw``: the platform's sales-rules boilerplate and
# the organizer's ad-tracking ids.
DROPPED_DETAIL_KEYS = ("eventDisclaimer", "trackingTags")
# Merchandising tags in ``categories`` that say nothing about the event's kind.
PROMO_CATEGORIES = frozenset({"nao-fique-de-fora"})

_EVENT_URL = re.compile(r"^https://www\.ingresse\.com/([a-z0-9][a-z0-9-]*)/?$")
_WS = re.compile(r"\s+")


def parse_search(body: str) -> list[dict[str, Any]]:
    """Return the events of one ``events/search`` page; ``[]`` for "No events found"."""
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("events/search response is not an object")
    events = data.get("events")
    if events is None and "detail" in data:
        return []
    if not isinstance(events, list):
        raise ValueError("events/search response has no events list")
    return [item for item in events if isinstance(item, dict)]


def category_of(categories: Any) -> str | None:
    """The first category slug that describes the event (``normalize.py`` maps it)."""
    for category in categories or []:
        slug = str(category.get("slug") or "") if isinstance(category, dict) else ""
        if slug and slug not in PROMO_CATEGORIES:
            return slug
    return None


def _text(html: str | None) -> str | None:
    """Plain text of an HTML fragment; block elements become line breaks."""
    if not html:
        return None
    text = HTMLParser(html).text(separator="\n").replace("\xa0", " ")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line) or None


def _parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 string with an offset and convert it to UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime from source: {value!r}")
    return dt.astimezone(UTC)


def _span(sessions: Any) -> tuple[datetime, datetime | None]:
    """Earliest and latest session start; no end for a single session.

    Sessions carry only their start, so a run of sessions ends at the last one's
    start and a single session is open-ended (the API's window rule, §7).
    """
    starts = sorted(
        _parse_utc(s["dateTime"])
        for s in sessions or []
        if isinstance(s, dict) and s.get("dateTime")
    )
    if not starts:
        raise ValueError("event has no session")
    return starts[0], starts[-1] if len(starts) > 1 else None


def _timezone(value: Any, state: str | None) -> str:
    """The event's own IANA zone when it is one, else the UF's."""
    if isinstance(value, str) and value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return value
    return timezone_for_uf(state)


def _address(place: dict[str, Any]) -> str | None:
    """``"Rodovia do Sol, Km26 - Guarapari-ES - 29208-720"`` from the place fields."""
    city, state = place.get("city"), place.get("state")
    parts = [
        _WS.sub(" ", str(place.get("street") or "")).strip(),
        f"{city}-{state}" if city and state else str(city or ""),
        str(place.get("zip") or ""),
    ]
    return " - ".join(p for p in parts if p) or None


def record_from_event(url: str, body: str) -> EventRecord:
    """EventRecord from ``events/<slug>``."""
    event: dict[str, Any] = json.loads(body)
    if not isinstance(event, dict) or not event.get("id"):
        raise ValueError("event response has no id")
    place: dict[str, Any] = event.get("place") or {}
    location: dict[str, Any] = place.get("location") or {}
    poster: dict[str, Any] = event.get("poster") or {}
    starts_at, ends_at = _span(event.get("sessions"))
    state = place.get("state") or None

    latitude: float | None = None
    longitude: float | None = None
    if location.get("lat") and location.get("lon"):
        latitude, longitude = float(location["lat"]), float(location["lon"])
    image = next(
        (str(poster[k]) for k in ("large", "xLarge", "medium") if poster.get(k)),
        None,
    )
    raw: dict[str, object] = {
        "event": {k: v for k, v in event.items() if k not in DROPPED_DETAIL_KEYS},
        # Prices need the tickets endpoint's apikey; not collected (ADR 0021).
        "price_lots_pending": True,
    }

    return EventRecord(
        source=IngresseAdapter.name,
        external_id=str(event["id"]),
        source_url=url,
        title=_WS.sub(" ", str(event["title"])).strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=_timezone(event.get("timezone"), state),
        venue_name=_WS.sub(" ", str(place.get("name") or "")).strip() or None,
        address=_address(place),
        city=place.get("city") or None,
        state=state,
        latitude=latitude,
        longitude=longitude,
        category=category_of(event.get("categories")),
        description=_text(event.get("description")),
        image_url=image,
        raw=raw,
    )


class IngresseAdapter(SourceAdapter):
    """Adapter for https://www.ingresse.com (JSON API at api-site.ingresse.com)."""

    name = "ingresse"
    base_url = "https://www.ingresse.com"
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        super().__init__(fetch)
        self._listings: dict[str, list[dict[str, Any]]] = {}  # UF -> listing, once per instance

    async def _listing(self, uf: str) -> list[dict[str, Any]]:
        """Every event of a state, page by page until a short or empty page."""
        if uf in self._listings:
            return self._listings[uf]
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            url = SEARCH_URL.format(state=uf, size=PAGE_SIZE, offset=page * PAGE_SIZE)
            try:
                events = parse_search(await self._fetch(self.name, url))
            except FetchError as exc:
                if exc.status != 404:  # 404 is "No events found": the end
                    raise
                events = []
            new = [e for e in events if str(e.get("id") or "") not in seen]
            seen.update(str(e.get("id") or "") for e in new)
            items.extend(new)
            if len(events) < PAGE_SIZE or not new:
                break
        self._listings[uf] = items
        log.info("ingresse.listing", state=uf, events=len(items))
        return items

    async def discover(self, city: City | None = None) -> AsyncIterator[str | Discovered]:
        """Yield canonical event URLs of the requested cities from their state-wide listings."""
        wanted: dict[str, set[str]] = {}  # UF -> folded place.city values
        for target in [city] if city is not None else for_source(self.name):
            name = target.key(self.name)
            if name is None:
                log.info("ingresse.city_not_listed", city=target.slug)
                continue
            wanted.setdefault(target.uf, set()).add(fold(name))
        emitted: set[str] = set()
        for uf, names in wanted.items():
            try:
                items = await self._listing(uf)
            except FetchError as exc:
                log.warning(
                    "ingresse.listing_unavailable", state=uf, url=exc.url, status=exc.status
                )
                continue
            except ValueError as exc:
                log.warning("ingresse.listing_unparsable", state=uf, error=str(exc))
                continue
            for item in items:
                event_id = str(item.get("id") or "")
                slug = str(item.get("slug") or "")
                place: dict[str, Any] = item.get("place") or {}
                if not event_id or not slug or event_id in emitted:
                    continue
                if place.get("state") != uf or fold(str(place.get("city") or "")) not in names:
                    continue
                emitted.add(event_id)
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
            raise ValueError(f"not an Ingresse event URL: {url}")
        slug = m.group(1)
        body = await self._fetch(self.name, EVENT_API_URL.format(slug=slug))
        return record_from_event(EVENT_URL.format(slug=slug), body)
