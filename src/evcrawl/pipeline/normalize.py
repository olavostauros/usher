"""Normalization of titles, dates, prices, categories and addresses (AGENTS.md §8).

``normalize(record)`` turns a source :class:`EventRecord` into a
:class:`NormalizedEvent`: the exact shape the upsert writes, with a
``content_hash`` that changes only when something a user would notice changed.
"""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from evcrawl.db.models import EventStatus
from evcrawl.logging import get_logger
from evcrawl.sources.base import EventRecord, PriceLot

log = get_logger("evcrawl.pipeline.normalize")

UNCATEGORIZED = "uncategorized"

# Internal taxonomy: slug -> display name. Keep it small; the PWA filters on it.
CATEGORIES: dict[str, str] = {
    "music": "Música",
    "party": "Festas",
    "theatre-and-dance": "Teatro, stand-up e dança",
    "course": "Cursos e workshops",
    "conference": "Congressos e palestras",
    "business": "Negócios e empreendedorismo",
    "academic": "Acadêmico e científico",
    "tech": "Tecnologia",
    "sports": "Esportes",
    "tour": "Passeios e tours",
    "food": "Gastronomia",
    "art-and-culture": "Arte, cinema e lazer",
    "wellness": "Saúde e bem-estar",
    "kids": "Infantil",
    "religion": "Religião e espiritualidade",
    "games": "Games e geek",
    "fashion": "Moda e beleza",
    UNCATEGORIZED: "Sem categoria",
}

# Source category slug -> internal slug. Unknown slugs land in ``uncategorized``
# with a warning so the table can grow from real data (AGENTS.md §8).
SOURCE_CATEGORIES: dict[str, dict[str, str]] = {
    "sympla": {
        "musica": "music",
        "festas-e-shows": "party",
        "teatro-stand-up-e-danca": "theatre-and-dance",
        "cursos-e-workshops": "course",
        "curso-e-workshop": "course",
        "congressos-e-palestras": "conference",
        "congresso-e-palestra": "conference",
        "empreendedorismo-negocios-e-inovacao": "business",
        "academico-e-cientifico": "academic",
        "informatica-tecnologia-e-programacao": "tech",
        "esportes": "sports",
        "passeios-e-tours": "tour",
        "gastronomia": "food",
        "arte-cinema-e-lazer": "art-and-culture",
        "saude-e-bem-estar": "wellness",
        "infantil": "kids",
        "religiao-e-espiritualidade": "religion",
        "games-e-geek": "games",
        "moda-e-beleza": "fashion",
        "outro": UNCATEGORIZED,
    },
    # ``events/categories`` of 2026-09-21 plus names seen on listings (ADR 0007).
    "blueticket": {
        "shows": "music",
        "festivais": "music",
        "baladas": "party",
        "carnaval": "party",
        "teatros": "theatre-and-dance",
        "stand-up": "theatre-and-dance",
        "infantil": "kids",
        "cursos-e-workshops": "course",
        "congressos": "conference",
        "esportivos": "sports",
        "exposicoes": "art-and-culture",
    },
    # ``categoryKey`` of the event page; the 14 keys of the site's table (ADR 0008).
    "ticketbolt": {
        "festas_shows": "party",
        "festas_juninas": "party",
        "teatros_espetaculos": "theatre-and-dance",
        "stand_up_comedy": "theatre-and-dance",
        "passeios_tours": "tour",
        "esportes": "sports",
        "congressos_palestras": "conference",
        "infantil": "kids",
        "cursos_workshops": "course",
        "gastronomia": "food",
        "pride": "party",
        "eventos_online": UNCATEGORIZED,
        "religiao_espiritualidade": "religion",
        "tecnologia": "tech",
    },
}

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# ``29050-662``, ``29050662`` and the dotted ``29.108.630`` organizers type by hand.
_POSTAL_CODE = re.compile(r"\b(\d{2})\.?(\d{3})[-.]?(\d{3})\b")


def collapse_whitespace(text: str) -> str:
    """Trim and collapse runs of whitespace to one space."""
    return _WS.sub(" ", text).strip()


def unaccent(text: str) -> str:
    """Drop diacritics: ``Vitória`` -> ``Vitoria``."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_key(text: str) -> str:
    """Lowercase, unaccented, alphanumeric words joined by single spaces (matching keys)."""
    return _NON_ALNUM.sub(" ", unaccent(text).lower()).strip()


def to_utc(value: datetime, timezone: str) -> datetime:
    """Return *value* in UTC; a naive datetime is read as local wall time in *timezone*."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone))
    return value.astimezone(UTC)


def map_category(source: str, slug: str | None) -> str:
    """Map a source category slug to the internal taxonomy; unknown -> ``uncategorized``."""
    if not slug:
        return UNCATEGORIZED
    mapped = SOURCE_CATEGORIES.get(source, {}).get(slug.strip().lower())
    if mapped is None:
        log.warning("normalize.unknown_category", source=source, category=slug)
        return UNCATEGORIZED
    return mapped


def postal_code(address: str | None) -> str | None:
    """``29050-662`` from anywhere in the address string, or None."""
    if not address:
        return None
    m = _POSTAL_CODE.search(address)
    return f"{m.group(1)}{m.group(2)}-{m.group(3)}" if m else None


class NormalizedVenue(BaseModel):
    name: str | None = None
    address: str | None = None
    address_normalized: str
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class NormalizedOrganizer(BaseModel):
    name: str
    name_normalized: str
    url: str | None = None


class NormalizedEvent(BaseModel):
    """What the upsert writes; ``content_hash`` covers every user-visible field."""

    source: str
    external_id: str
    source_url: str
    title: str
    title_normalized: str
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    timezone: str
    status: EventStatus
    is_online: bool = False
    image_url: str | None = None
    venue: NormalizedVenue | None = None
    organizer: NormalizedOrganizer | None = None
    category: str
    category_source: str | None = None
    price_lots: list[PriceLot] = Field(default_factory=list)
    price_lots_pending: bool = False
    content_hash: str
    raw: dict[str, object] = Field(default_factory=dict)


def _venue(record: EventRecord) -> NormalizedVenue | None:
    name = collapse_whitespace(record.venue_name or "") or None
    address = collapse_whitespace(record.address or "") or None
    key_source = address or " ".join(p for p in (name, record.city, record.state) if p)
    if not key_source:
        return None
    return NormalizedVenue(
        name=name,
        address=address,
        address_normalized=normalize_key(key_source),
        city=collapse_whitespace(record.city or "") or None,
        state=(record.state or "").strip().upper() or None,
        postal_code=postal_code(address),
        latitude=record.latitude,
        longitude=record.longitude,
    )


def _organizer(record: EventRecord) -> NormalizedOrganizer | None:
    name = collapse_whitespace(record.organizer or "")
    if not name:
        return None
    return NormalizedOrganizer(name=name, name_normalized=normalize_key(name))


def _price_lots(record: EventRecord) -> tuple[list[PriceLot], bool]:
    lots: list[PriceLot] = []
    seen: set[tuple[str, Decimal, str]] = set()
    for lot in record.price_lots:
        amount = lot.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        key = (collapse_whitespace(lot.name), amount, lot.currency.upper())
        if key in seen:
            continue
        seen.add(key)
        lots.append(PriceLot(name=key[0], amount=amount, currency=key[2]))
    pending = bool(record.raw.get("price_lots_pending")) or not lots
    return lots, pending


def _status(
    record: EventRecord, starts_at: datetime, ends_at: datetime | None, now: datetime
) -> EventStatus:
    if record.cancelled:
        return EventStatus.CANCELLED
    if record.closed or (ends_at or starts_at) < now:
        return EventStatus.ENDED
    return EventStatus.SCHEDULED


def content_hash(event: dict[str, object]) -> str:
    """sha256 of the canonical JSON of the user-visible fields."""
    canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize(record: EventRecord, *, now: datetime | None = None) -> NormalizedEvent:
    """Normalize one source record (pure; the only side effect is the unknown-category log)."""
    now = now or datetime.now(UTC)
    starts_at = to_utc(record.starts_at, record.timezone)
    ends_at = to_utc(record.ends_at, record.timezone) if record.ends_at else None
    title = collapse_whitespace(record.title)
    venue = _venue(record)
    organizer = _organizer(record)
    lots, pending = _price_lots(record)
    status = _status(record, starts_at, ends_at, now)
    category = map_category(record.source, record.category)
    # Descriptions keep their line breaks (paragraphs); only the edges are trimmed.
    description = (record.description or "").strip() or None
    image_url = str(record.image_url) if record.image_url else None
    is_online = bool(record.raw.get("online")) or venue is None

    content: dict[str, object] = {
        "title": title,
        "description": description,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat() if ends_at else None,
        "timezone": record.timezone,
        "status": status.value,
        "is_online": is_online,
        "image_url": image_url,
        "venue": venue.model_dump() if venue else None,
        "organizer": organizer.model_dump() if organizer else None,
        "category": category,
        "price_lots": [(lot.name, str(lot.amount), lot.currency) for lot in lots],
        "price_lots_pending": pending,
    }
    return NormalizedEvent(
        source=record.source,
        external_id=record.external_id,
        source_url=str(record.source_url),
        title=title,
        title_normalized=normalize_key(title),
        description=description,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=record.timezone,
        status=status,
        is_online=is_online,
        image_url=image_url,
        venue=venue,
        organizer=organizer,
        category=category,
        category_source=record.category,
        price_lots=lots,
        price_lots_pending=pending,
        content_hash=content_hash(content),
        raw=record.raw,
    )


__all__ = [
    "CATEGORIES",
    "SOURCE_CATEGORIES",
    "UNCATEGORIZED",
    "NormalizedEvent",
    "NormalizedOrganizer",
    "NormalizedVenue",
    "collapse_whitespace",
    "content_hash",
    "map_category",
    "normalize",
    "normalize_key",
    "postal_code",
    "to_utc",
    "unaccent",
]
