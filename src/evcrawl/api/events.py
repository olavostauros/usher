"""``/v1/events``: the search's parameters, its SQL on ``api_events``, and the cursor.

Spec §3 "Endpoints" and "Pagination". A search is a ``Search`` (validated
parameters), which ``list_sql`` turns into one ``SELECT`` on ``api_events``:

- the filters (radius with ``ST_DWithin`` so ``ix_venues_location`` serves it,
  city, window, categories, free, online, title words);
- recurring sessions collapsed (owner's decision, spec §3): rows with the
  same venue and the same normalized title once a leading time-of-day prefix
  (``17h``, ``9h30``, ``17 00``, then an optional ``horario local``) is
  removed are one group; the group's earliest row in the window stands for it,
  with ``sessions`` = the group's size. Events without a venue never group;
- keyset pagination on ``(distance_m, id)`` or ``(starts_at, id)``, the
  cursor an opaque base64 of the last row's keys, never an offset.

Nothing here knows HTTP; ``routes/events.py`` does.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from evcrawl.pipeline.normalize import normalize_key

DEFAULT_RADIUS_KM = 25.0
MAX_RADIUS_KM = 100.0
DEFAULT_WINDOW = timedelta(days=30)
MAX_WINDOW = timedelta(days=180)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_CATEGORIES = 10
MAX_QUERY_WORDS = 6

Online = Literal["include", "only", "exclude"]
Sort = Literal["distance", "starts_at"]

# On title_normalized (lowercase, unaccented, alphanumeric words): "17h", "9h30",
# "17hs", "17 00" (from "17:00"), then an optional "horario local" or "horario
# de brasilia". Python's ``re`` and PostgreSQL's ARE read it the same way.
SERIES_PREFIX = (
    r"^(?:\d{1,2}\s?(?:h|hs|hrs)(?:\s?[0-5]\d)?|\d{1,2}\s[0-5]\d)\s+"
    r"(?:horario (?:local|de brasilia)\s+)?"
)

_POINT = "st_setsrid(st_makepoint(:lon, :lat), 4326)::geography"
_ITEM_COLUMNS = (
    "id, title, starts_at, ends_at, timezone, is_online, image_url, category, category_name, "
    "venue_id, venue_name, venue_address, venue_city, venue_uf, lat, lon, "
    "price_min, price_max, currency, is_free, sources, updated_at"
)


class InvalidSearch(ValueError):
    """A parameter the route must answer with 422 ``invalid_parameter`` on ``field``."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class Cursor:
    """The last row's sort keys. ``distance_m`` is ``None`` past the located rows."""

    sort: Sort
    id: int
    distance_m: int | None = None
    starts_at: datetime | None = None

    def encode(self) -> str:
        payload: dict[str, Any] = {"v": 1, "k": self.sort[0], "id": self.id}
        if self.sort == "distance":
            payload["d"] = self.distance_m
        else:
            assert self.starts_at is not None
            payload["s"] = self.starts_at.astimezone(UTC).isoformat()
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @classmethod
    def decode(cls, text: str, sort: Sort) -> "Cursor":
        try:
            padded = text + "=" * (-len(text) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if payload.get("v") != 1 or payload.get("k") != sort[0]:
                raise ValueError("cursor is for another sort")
            event_id = int(payload["id"])
            if sort == "distance":
                d = payload["d"]
                return cls(sort, event_id, distance_m=None if d is None else int(d))
            return cls(sort, event_id, starts_at=datetime.fromisoformat(payload["s"]))
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
            raise InvalidSearch("cursor", "cursor is not one this search returned") from exc


@dataclass(frozen=True)
class Search:
    """A validated ``/v1/events`` request (spec §3): build one with ``Search.build``."""

    lat: float | None
    lon: float | None
    radius_km: float
    city: str | None
    window_from: datetime
    window_to: datetime
    categories: tuple[str, ...]
    free: bool | None
    online: Online
    words: tuple[str, ...]
    sort: Sort
    limit: int
    cursor: Cursor | None

    @property
    def located(self) -> bool:
        return self.lat is not None

    @classmethod
    def build(
        cls,
        *,
        lat: float | None,
        lon: float | None,
        radius_km: float | None,
        city: str | None,
        window_from: str | None,
        window_to: str | None,
        categories: list[str] | None,
        free: bool | None,
        online: Online | None,
        q: str | None,
        sort: Sort | None,
        limit: int,
        cursor: str | None,
        now: datetime | None = None,
    ) -> "Search":
        if (lat is None) != (lon is None):
            missing = "lat" if lat is None else "lon"
            raise InvalidSearch(missing, "lat and lon go together")
        located = lat is not None
        if not located and city is None:
            raise InvalidSearch("city", "either lat and lon, or city, is required")
        if radius_km is not None and not located:
            raise InvalidSearch("radius_km", "radius_km needs lat and lon")
        if sort == "distance" and not located:
            raise InvalidSearch("sort", "sort=distance needs lat and lon")
        if city is not None:
            city = _registry_slug(city)
        start, end = _window(window_from, window_to, now or datetime.now(UTC))
        chosen_sort: Sort = sort or ("distance" if located else "starts_at")
        return cls(
            lat=lat,
            lon=lon,
            radius_km=radius_km if radius_km is not None else DEFAULT_RADIUS_KM,
            city=city,
            window_from=start,
            window_to=end,
            categories=_categories(categories),
            free=free,
            online=online or ("exclude" if located else "include"),
            words=_words(q),
            sort=chosen_sort,
            limit=limit,
            cursor=Cursor.decode(cursor, chosen_sort) if cursor else None,
        )


def _registry_slug(city: str) -> str:
    from evcrawl import cities

    try:
        return cities.by_slug(city).slug
    except ValueError:
        raise InvalidSearch("city", f"unknown city {city!r}; see /v1/cities") from None


def _categories(values: list[str] | None) -> tuple[str, ...]:
    slugs = tuple(dict.fromkeys(v.strip().lower() for v in values or () if v.strip()))
    if len(slugs) > MAX_CATEGORIES:
        raise InvalidSearch("category", f"at most {MAX_CATEGORIES} categories")
    for slug in slugs:
        if not all(ch.isalnum() or ch in "-_" for ch in slug) or len(slug) > 64:
            raise InvalidSearch("category", f"{slug!r} is not a category slug; see /v1/categories")
    return slugs


def _words(q: str | None) -> tuple[str, ...]:
    if q is None:
        return ()
    words = tuple(normalize_key(q).split())[:MAX_QUERY_WORDS]
    if not words:
        raise InvalidSearch("q", "q needs at least one letter or digit")
    return words


def _instant(value: str, field: str, *, end_of_day: bool) -> datetime:
    """An ISO date (a whole UTC day) or datetime (naive means UTC)."""
    text = value.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return start + timedelta(days=1) if end_of_day else start
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidSearch(field, "an ISO 8601 date or datetime") from None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _window(start: str | None, end: str | None, now: datetime) -> tuple[datetime, datetime]:
    lo = _instant(start, "from", end_of_day=False) if start else now
    hi = _instant(end, "to", end_of_day=True) if end else lo + DEFAULT_WINDOW
    if hi <= lo:
        raise InvalidSearch("to", "to must be after from")
    if hi - lo > MAX_WINDOW:
        raise InvalidSearch("to", f"from..to spans more than {MAX_WINDOW.days} days")
    return lo, hi


def _like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def list_sql(search: Search) -> tuple[str, dict[str, Any]]:
    """The ``SELECT`` for one page of ``search`` (``limit + 1`` rows, to know if more follow)."""
    params: dict[str, Any] = {
        "from_": search.window_from,
        "to_": search.window_to,
        "series_re": SERIES_PREFIX,
        "limit": search.limit + 1,
    }
    where = ["starts_at < :to_", "coalesce(ends_at, starts_at) >= :from_"]
    if search.located:
        params.update(lat=search.lat, lon=search.lon, radius_m=search.radius_km * 1000)
        within = f"st_dwithin(location, {_POINT}, :radius_m)"
        distance = f"round(st_distance(location, {_POINT}))::bigint"
        where.append(
            {
                "exclude": f"{within} and not is_online",
                "include": f"({within} or is_online)",
                "only": "is_online",
            }[search.online]
        )
    else:
        distance = "null::bigint"
        params["city"] = search.city
        where.append(":city = any(cities)")
        if search.online != "include":
            where.append("is_online" if search.online == "only" else "not is_online")
    if search.categories:
        params["categories"] = list(search.categories)
        where.append("category = any(:categories)")
    if search.free is True:
        where.append("is_free")
    elif search.free is False:
        where.append("is_free is distinct from true")
    for i, word in enumerate(search.words):
        params[f"w{i}"] = _like(word)
        where.append(f"title_normalized like :w{i}")

    if search.sort == "distance":
        order = "distance_m nulls last, id"
        keyset = _distance_keyset(search.cursor, params)
    else:
        order = "starts_at, id"
        keyset = _starts_at_keyset(search.cursor, params)

    sql = f"""
with candidates as (
    select {_ITEM_COLUMNS},
           {distance} as distance_m,
           regexp_replace(title_normalized, :series_re, '') as series_key
      from api_events
     where {" and ".join(where)}
), ranked as (
    select c.*,
           count(*) over w as sessions,
           row_number() over (w order by starts_at, id) as session_rank
      from candidates c
    window w as (partition by case when venue_id is null then id else 0 end, venue_id, series_key)
)
select * from ranked
 where session_rank = 1{keyset}
 order by {order}
 limit :limit
"""
    return sql, params


def _distance_keyset(cursor: Cursor | None, params: dict[str, Any]) -> str:
    if cursor is None:
        return ""
    params["after_id"] = cursor.id
    if cursor.distance_m is None:
        return " and distance_m is null and id > :after_id"
    params["after_d"] = cursor.distance_m
    return (
        " and (distance_m > :after_d or (distance_m = :after_d and id > :after_id)"
        " or distance_m is null)"
    )


def _starts_at_keyset(cursor: Cursor | None, params: dict[str, Any]) -> str:
    if cursor is None:
        return ""
    params["after_id"] = cursor.id
    params["after_s"] = cursor.starts_at
    return " and (starts_at, id) > (:after_s, :after_id)"


def next_cursor(search: Search, last: dict[str, Any]) -> str:
    """The cursor for the page after the one ending in row ``last``."""
    if search.sort == "distance":
        d = last["distance_m"]
        return Cursor(
            "distance", int(last["id"]), distance_m=None if d is None else int(d)
        ).encode()
    return Cursor("starts_at", int(last["id"]), starts_at=last["starts_at"]).encode()


DETAIL_SQL = (
    f"select {_ITEM_COLUMNS}, description, organizer_name, organizer_url"
    " from api_events where id = :id"
)
PRICES_SQL = (
    "select name, amount, currency from api_event_prices"
    " where event_id = :id order by position, name"
)
STATUS_SQL = "select status from api_event_status where id = :id"
