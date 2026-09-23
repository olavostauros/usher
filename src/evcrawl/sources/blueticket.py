"""BlueTicket adapter (cities from the registry, ``evcrawl.cities``).

Data acquisition follows ``docs/specs/phase-2-sources.md`` §2/§4 and ADR 0007:

- the site is a client-rendered Nuxt app; every page is the same shell, so the
  JSON API behind it is the only data source;
- ``events/?state=<UF>`` lists a whole state in one response (no pagination,
  ``city=`` is ignored server-side), fetched once per UF per adapter instance;
  ``discover()`` filters by ``cityName``;
- ``events/<id>/detail`` adds description, category, venue (coordinates when the
  organizer filled them in), organizer and media;
- price lots come from the store API, ``pdv/v3/products/sectors/<id>``.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from selectolax.parser import HTMLParser

from evcrawl.cities import City, fold, for_source, timezone_for_uf
from evcrawl.logging import get_logger
from evcrawl.pipeline.fetch import FetchError, FetchText, fetch_text
from evcrawl.sources.base import EventRecord, PriceLot, RateLimit, SourceAdapter

log = get_logger("evcrawl.sources.blueticket")

API = "https://api2.blueticket.com.br"
STORE_API = "https://soulapi.blueticket.com.br/api"
LISTING_URL = API + "/events/?state={state}"
DETAIL_URL = API + "/events/{id}/detail"
# ``pdv``/``pos`` 100 is the public web store (``store/configs``); the site sends
# them as headers, the endpoint accepts them as query parameters too.
PRODUCTS_URL = STORE_API + "/pdv/v3/products/sectors/{id}?event={id}&pdv=100&pos=100"
EVENT_URL = "https://www.blueticket.com.br/evento/{id}"

_EVENT_URL = re.compile(r"^https://www\.blueticket\.com\.br/evento/(\d+)(?:/[^/]*)?/?$")
_WS = re.compile(r"\s+")


def category_slug(name: str | None) -> str | None:
    """``"Cursos e Workshops"`` -> ``"cursos-e-workshops"`` (the key ``normalize.py`` maps)."""
    if not name:
        return None
    return re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-") or None


def parse_listing(body: str) -> list[dict[str, Any]]:
    """Return the event objects of one ``events/?state=`` response."""
    data = json.loads(body)
    if not isinstance(data, list):
        raise ValueError("events listing is not a list")
    return [item for item in data if isinstance(item, dict)]


def _from_ts(value: Any) -> datetime | None:
    """UTC datetime from the API's epoch seconds; ``None`` for 0/missing."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, UTC) if ts > 0 else None


def _text(html: str | None) -> str | None:
    """Plain text of an HTML fragment; block elements become line breaks."""
    if not html:
        return None
    text = HTMLParser(html).text(separator="\n").replace("\xa0", " ")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line) or None


def _money(value: Any) -> Decimal:
    """``25`` / ``2.5`` / ``"27.00"`` -> Decimal, exact to the cent."""
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


def parse_products(body: str) -> list[PriceLot]:
    """Price lots from ``products/sectors``: ``valor`` + ``valor_conveniencia``, the buyer's price.

    Only tickets (``tipo_produto == "I"``) count; closed lots (``encerrado``)
    are skipped, sold-out ones are kept (the price still stands). The name is
    ``<sector or product> - <type>`` so ``Inteira``/``Meia`` stay distinct.
    """
    data = json.loads(body)
    lots: list[PriceLot] = []
    for sector in (data.get("sectors") if isinstance(data, dict) else None) or []:
        if not isinstance(sector, dict):
            continue
        sector_name = str(sector.get("nome") or "").strip()
        for product in sector.get("products") or []:
            if not isinstance(product, dict) or product.get("encerrado"):
                continue
            if product.get("tipo_produto") not in (None, "I"):
                continue
            if product.get("valor") is None:
                continue
            base = _WS.sub(" ", sector_name or str(product.get("nome") or "Ingresso")).strip()
            kind = _WS.sub(" ", str(product.get("nome_tipo") or "")).strip()
            name = f"{base} - {kind}" if kind and kind.lower() != base.lower() else base
            amount = _money(product["valor"]) + _money(product.get("valor_conveniencia"))
            lots.append(PriceLot(name=name, amount=amount))
    return lots


def _strip_contact(detail: dict[str, Any]) -> dict[str, Any]:
    """Copy of a detail object without free-text organizer fields and tracking tokens."""
    clean = {k: v for k, v in detail.items() if k != "integrations"}
    organizer = detail.get("organizer")
    if isinstance(organizer, dict):
        clean["organizer"] = {k: v for k, v in organizer.items() if k != "description"}
    return clean


def record_from_detail(url: str, body: str, lots: list[PriceLot] | None) -> EventRecord:
    """EventRecord from ``events/<id>/detail``; ``lots`` is None when the products call failed."""
    detail: dict[str, Any] = json.loads(body)
    if not isinstance(detail, dict) or not detail.get("id"):
        raise ValueError("detail response has no event id")
    info: dict[str, Any] = detail.get("info") or {}
    dates: dict[str, Any] = detail.get("dates") or {}
    sale: dict[str, Any] = detail.get("sale") or {}
    venue: dict[str, Any] = detail.get("venue") or {}
    organizer: dict[str, Any] = detail.get("organizer") or {}
    category: dict[str, Any] = info.get("category") or {}
    # ``media`` mixes images (type 1, absolute URL) with videos (type 4, a YouTube id).
    images = [
        str(m["url"])
        for m in detail.get("media") or []
        if isinstance(m, dict) and str(m.get("url") or "").startswith("https://")
    ]

    starts_at = _from_ts(dates.get("dateTs"))
    if starts_at is None:
        raise ValueError("detail has no dateTs")
    # 0/0 means "not filled in", not the Gulf of Guinea; leave it to the geocoder.
    latitude: float | None = None
    longitude: float | None = None
    if venue.get("latitude") and venue.get("longitude"):
        latitude, longitude = float(venue["latitude"]), float(venue["longitude"])

    raw: dict[str, object] = {"event": _strip_contact(detail), "products_fetched": lots is not None}
    if lots is None:
        raw["price_lots_pending"] = True
    if venue.get("type") == "online":
        raw["online"] = True

    return EventRecord(
        source=BlueTicketAdapter.name,
        external_id=str(detail["id"]),
        source_url=url,
        title=str(info["name"]).strip(),
        starts_at=starts_at,
        timezone=timezone_for_uf(venue.get("state")),
        venue_name=venue.get("name") or None,
        address=_WS.sub(" ", str(venue.get("address") or "")).strip() or None,
        city=venue.get("city") or None,
        state=venue.get("state") or None,
        latitude=latitude,
        longitude=longitude,
        organizer=organizer.get("name") or None,
        category=category_slug(category.get("name")),
        description=_text(info.get("description")),
        image_url=images[0] if images else None,
        price_lots=list(lots or []),
        cancelled=bool(sale.get("isCancelled")),
        raw=raw,
    )


class BlueTicketAdapter(SourceAdapter):
    """Adapter for https://www.blueticket.com.br (JSON API at api2.blueticket.com.br)."""

    name = "blueticket"
    base_url = "https://www.blueticket.com.br"
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        super().__init__(fetch)
        self._listings: dict[str, list[dict[str, Any]]] = {}  # UF -> listing, once per instance

    async def _listing(self, uf: str) -> list[dict[str, Any]]:
        if uf not in self._listings:
            items = parse_listing(await self._fetch(self.name, LISTING_URL.format(state=uf)))
            self._listings[uf] = items
            log.info("blueticket.listing", state=uf, events=len(items))
        return self._listings[uf]

    async def discover(self, city: City | None = None) -> AsyncIterator[str]:
        """Yield canonical event URLs of the requested cities from their state-wide listings."""
        wanted: dict[str, set[str]] = {}  # UF -> folded cityName values
        for target in [city] if city is not None else for_source(self.name):
            name = target.key(self.name)
            if name is None:
                log.info("blueticket.city_not_listed", city=target.slug)
                continue
            wanted.setdefault(target.uf, set()).add(fold(name))
        seen: set[str] = set()
        for uf, names in wanted.items():
            url = LISTING_URL.format(state=uf)
            try:
                items = await self._listing(uf)
            except FetchError as exc:
                log.warning("blueticket.listing_unavailable", url=url, status=exc.status)
                continue
            except ValueError as exc:
                log.warning("blueticket.listing_unparsable", url=url, error=str(exc))
                continue
            for item in items:
                event_id = str(item.get("id") or "")
                if not event_id or event_id in seen:
                    continue
                if item.get("cityState") != uf:
                    continue
                if fold(str(item.get("cityName") or "")) not in names:
                    continue
                seen.add(event_id)
                yield EVENT_URL.format(id=event_id)

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        m = _EVENT_URL.match(url)
        if m is None:
            raise ValueError(f"not a BlueTicket event URL: {url}")
        event_id = m.group(1)
        body = await self._fetch(self.name, DETAIL_URL.format(id=event_id))
        lots: list[PriceLot] | None
        try:
            lots = parse_products(await self._fetch(self.name, PRODUCTS_URL.format(id=event_id)))
        except (FetchError, ValueError) as exc:
            log.warning("blueticket.products_unavailable", url=url, error=str(exc))
            lots = None
        record = record_from_detail(EVENT_URL.format(id=event_id), body, lots)
        if record.external_id != event_id:
            log.warning("blueticket.id_mismatch", url=url, api_id=record.external_id)
        return record
