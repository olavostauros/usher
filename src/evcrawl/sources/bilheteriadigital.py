"""Bilheteria Digital adapter (cities from the registry, ``evcrawl.cities``).

Data acquisition follows ``docs/SPECIFICATION.md`` §2 and ADR 0023:

- the site is server-rendered PHP with no JSON API, embedded JSON or JSON-LD;
- ``/<UF>/as/1`` is the HTML fragment the site's state picker loads
  (``eventosPorEstado`` in ``/assets/busca.js``): every event of the state on
  one page (``/<UF>/as/2`` is 404); cards are ``li.box-li-evento`` with the
  event path, title, a day and month without year, ``"<city> - <UF>"`` and the
  venue; ``discover()`` filters by city;
- the event page ``/<slug>`` carries the start as UTC in a hidden
  ``dataInicio`` input, the coordinates in ``input#coordenadas``, the
  description, and one row per ticket with its price and fee in ``data-``
  attributes; a calendar-mode event (an exhibition with daily sessions) adds
  ``window.BD_CALENDARIO`` with every session and may be served from
  ``checkout2.bilheteriadigital.com`` after a redirect;
- the site shows every time in Brasília time (``dataInicio`` is that wall
  time in UTC), so the start is read as the wall time the site shows,
  placed in the UF's zone.

The listing path (the slug) is the external id: it is what the card links
to and what ``fetch()`` requests, so ``discover()`` knows it without the page.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

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

log = get_logger("evcrawl.sources.bilheteriadigital")

BASE = "https://www.bilheteriadigital.com"
LISTING_URL = BASE + "/{state}/as/1"
EVENT_URL = BASE + "/{slug}"
# The zone every time on the site is shown in (ADR 0023).
SITE_ZONE = ZoneInfo("America/Sao_Paulo")
MONTHS = {
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_EVENT_URL = re.compile(r"^https://www\.bilheteriadigital\.com/([A-Za-z0-9][A-Za-z0-9_-]*)$")
_CALENDAR = re.compile(r"window\.BD_CALENDARIO\s*=\s*")
# A calendar event's ticket names carry the session: "13/10- INGRESSO INTEIRA | SESSÃO 14h00".
_LOT_DATE = re.compile(r"^\d{1,2}/\d{1,2}\s*-\s*")
_LOT_SESSION = re.compile(r"\s*\|\s*SESS[ÃA]O\s+\d{1,2}h\d{0,2}\s*$", re.I)
_ADDRESS = re.compile(r"^endere[çc]o\s*:\s*(.+)$", re.I)
_WS = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


def _node_text(node: Node | None) -> str:
    return _clean(node.text()) if node is not None else ""


def split_city(text: str) -> tuple[str, str]:
    """``"Cariacica - ES"`` -> ``("Cariacica", "ES")``; ``("", "")`` when there is no UF."""
    city, sep, uf = _clean(text).rpartition(" - ")
    if not sep or len(uf) != 2:
        return "", ""
    return city.strip(), uf.upper()


def parse_listing(body: str) -> list[dict[str, str]]:
    """The event cards of one ``/<UF>/as/1`` fragment, in page order."""
    items: list[dict[str, str]] = []
    for card in HTMLParser(body).css("li.box-li-evento"):
        link = card.css_first("a[href]")
        href = str(link.attributes.get("href") or "") if link is not None else ""
        slug = href.strip().removeprefix("/")
        if not href.startswith("/") or not _SLUG.match(slug):  # the mustache template, links out
            continue
        city, state = split_city(_node_text(card.css_first("div.cidade-box-evento")))
        image = card.css_first("img")
        items.append(
            {
                "slug": slug,
                "title": _node_text(card.css_first("p.titulo-evento-thumb")),
                "date": _node_text(card.css_first("p.data-evento-thumb")),
                "city": city,
                "state": state,
                "venue": _node_text(card.css_first("div.local-box-evento")),
                "image": str(image.attributes.get("src") or "") if image is not None else "",
            }
        )
    return items


def _input(tree: HTMLParser, name: str) -> str:
    node = tree.css_first(f'input[name="{name}"]')
    return _clean(node.attributes.get("value")) if node is not None else ""


def _local(wall: datetime, zone: str) -> datetime:
    """A naive wall time in *zone*, as UTC."""
    return wall.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)


def _start(value: str, zone: str) -> datetime:
    """``dataInicio`` (the Brasília wall time, in UTC) as the event's own wall time in *zone*."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive dataInicio: {value!r}")
    wall = dt.astimezone(SITE_ZONE).replace(tzinfo=None)
    return _local(wall, zone)


def calendar_sessions(body: str) -> list[datetime]:
    """Every session of a calendar-mode event, as naive local wall times, sorted."""
    m = _CALENDAR.search(body)
    if m is None:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(body, m.end())
    except ValueError:
        return []
    sessions = data.get("sessoes") if isinstance(data, dict) else None
    out: list[datetime] = []
    for day, times in (sessions or {}).items() if isinstance(sessions, dict) else []:
        for hhmm in times if isinstance(times, dict) else []:
            try:
                out.append(datetime.fromisoformat(f"{day}T{hhmm}"))
            except ValueError:
                continue
    return sorted(out)


def _range_end(tree: HTMLParser, start_wall: datetime) -> datetime | None:
    """The second date of a ``30 Out A 01 Nov`` header, at the start's wall time."""
    dates = tree.css("div.topo-data-group div.topo-data")
    if len(dates) < 2:
        return None
    day = _node_text(dates[1].css_first("span.data"))
    month = MONTHS.get(fold(_node_text(dates[1].css_first("span.mes")))[:3])
    if not day.isdigit() or month is None:
        return None
    year = start_wall.year
    try:
        end = date(year, month, int(day))
        if end < start_wall.date():
            end = date(year + 1, month, int(day))
    except ValueError:
        return None
    if end == start_wall.date():
        return None
    return datetime.combine(end, start_wall.time())


def _coordinates(tree: HTMLParser) -> tuple[float | None, float | None]:
    node = tree.css_first("input#coordenadas")
    value = str(node.attributes.get("value") or "") if node is not None else ""
    lat, _, lng = value.partition(",")
    try:
        latitude, longitude = float(lat), float(lng)
    except ValueError:
        return None, None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or (latitude, longitude) == (0, 0):
        return None, None
    return latitude, longitude


def _description(tree: HTMLParser) -> str | None:
    node = tree.css_first("div.informacoes-adicionais-evento")
    if node is None:
        return None
    lines = [_clean(line) for line in node.text(separator="\n").splitlines()]
    return "\n".join(line for line in lines if line) or None


def _address(description: str | None) -> str | None:
    """The organizer's ``Endereço: ...`` line in the description, when there is one."""
    for line in (description or "").splitlines():
        m = _ADDRESS.match(line)
        if m:
            return _clean(m.group(1)) or None
    return None


def _money(value: str | None) -> Decimal:
    return Decimal(str(value or "0").strip() or "0")


def lot_name(text: str) -> str:
    """Ticket name without a calendar event's session prefix and suffix."""
    return _clean(_LOT_SESSION.sub("", _LOT_DATE.sub("", _clean(text))))


def price_lots(tree: HTMLParser) -> list[PriceLot]:
    """One lot per ticket row: ``data-ingresso-valor`` + ``data-ingresso-taxa``, the buyer's price.

    A calendar event repeats each ticket for every session; the session is
    stripped from the name and repeats are dropped.
    """
    lots: list[PriceLot] = []
    seen: set[tuple[str, Decimal]] = set()
    for row in tree.css("div.linhaIngressoTr[data-ingresso-id]"):
        name = lot_name(_node_text(row.css_first("p.ingresso-titulo"))) or "Ingresso"
        try:
            amount = _money(row.attributes.get("data-ingresso-valor")) + _money(
                row.attributes.get("data-ingresso-taxa")
            )
        except InvalidOperation:
            continue
        amount = amount.quantize(Decimal("0.01"))
        if amount < 0 or (name, amount) in seen:
            continue
        seen.add((name, amount))
        lots.append(PriceLot(name=name, amount=amount))
    return lots


def record_from_page(url: str, body: str) -> EventRecord:
    """EventRecord from an event page; *url* is the canonical ``/<slug>`` URL."""
    m = _EVENT_URL.match(url)
    if m is None:
        raise ValueError(f"not a Bilheteria Digital event URL: {url}")
    slug = m.group(1)
    tree = HTMLParser(body)
    event_id = _input(tree, "eventoId")
    start_value = _input(tree, "dataInicio")
    if not event_id or not start_value:
        raise ValueError(f"not an event page: {url}")

    info = tree.css_first("h3.textinfo")
    city_line = ""
    if info is not None:
        city_line = next((p for p in (_clean(t) for t in info.text().splitlines()) if p), "")
    city, state = split_city(city_line)
    zone = timezone_for_uf(state)
    starts_at = _start(start_value, zone)
    start_wall = starts_at.astimezone(ZoneInfo(zone)).replace(tzinfo=None)

    sessions = calendar_sessions(body)
    end_wall = sessions[-1] if sessions else _range_end(tree, start_wall)
    ends_at = _local(end_wall, zone) if end_wall is not None else None
    if ends_at is not None and ends_at <= starts_at:
        ends_at = None

    latitude, longitude = _coordinates(tree)
    description = _description(tree)
    image = tree.css_first('meta[property="og:image"]')
    image_url = str(image.attributes.get("content") or "") if image is not None else ""
    title = _input(tree, "nomeEvento") or _node_text(tree.css_first("h1.title"))
    venue = _input(tree, "local") or _node_text(info.css_first("span.small") if info else None)
    raw: dict[str, object] = {
        "evento_id": event_id,
        "slug": slug,
        "estilo_id": _input(tree, "estiloId") or None,
        "data_inicio": start_value,
        "cidade": city_line,
        "local": venue,
        "coordenadas": [latitude, longitude] if latitude is not None else None,
        "sessoes": len(sessions),
    }

    return EventRecord(
        source=BilheteriaDigitalAdapter.name,
        external_id=slug,
        source_url=url,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=zone,
        venue_name=venue or None,
        address=_address(description),
        city=city or None,
        state=state or None,
        latitude=latitude,
        longitude=longitude,
        description=description,
        image_url=image_url if image_url.startswith("https://") else None,
        price_lots=price_lots(tree),
        raw=raw,
    )


class BilheteriaDigitalAdapter(SourceAdapter):
    """Adapter for https://www.bilheteriadigital.com (server-rendered HTML)."""

    name = "bilheteriadigital"
    base_url = BASE
    rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)

    def __init__(self, fetch: FetchText = fetch_text) -> None:
        super().__init__(fetch)
        self._listings: dict[str, list[dict[str, str]]] = {}  # UF -> cards, once per instance

    async def _listing(self, uf: str) -> list[dict[str, str]]:
        if uf not in self._listings:
            items = parse_listing(await self._fetch(self.name, LISTING_URL.format(state=uf)))
            self._listings[uf] = items
            log.info("bilheteriadigital.listing", state=uf, events=len(items))
        return self._listings[uf]

    async def discover(self, city: City | None = None) -> AsyncIterator[str | Discovered]:
        """Yield canonical event URLs of the requested cities from their state listings."""
        wanted: dict[str, set[str]] = {}  # UF -> folded city names
        for target in [city] if city is not None else for_source(self.name):
            name = target.key(self.name)
            if name is None:
                log.info("bilheteriadigital.city_not_listed", city=target.slug)
                continue
            wanted.setdefault(target.uf, set()).add(fold(name))
        emitted: set[str] = set()
        for uf, names in wanted.items():
            try:
                items = await self._listing(uf)
            except FetchError as exc:
                log.warning(
                    "bilheteriadigital.listing_unavailable",
                    state=uf,
                    url=exc.url,
                    status=exc.status,
                )
                continue
            for item in items:
                slug = item["slug"]
                if slug in emitted or item["state"] != uf or fold(item["city"]) not in names:
                    continue
                emitted.add(slug)
                yield Discovered(EVENT_URL.format(slug=slug), slug, fingerprint(item))

    async def fetch(self, external_id: str) -> EventRecord:
        """Return one event; *external_id* is the canonical URL yielded by :meth:`discover`."""
        url = external_id
        if _EVENT_URL.match(url) is None:
            raise ValueError(f"not a Bilheteria Digital event URL: {url}")
        return record_from_page(url, await self._fetch(self.name, url))
