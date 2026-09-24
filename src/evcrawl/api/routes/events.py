"""``/v1/events`` and ``/v1/events/{id}``: "events near me" and one event (spec §3)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError

from evcrawl.api.db import Reader
from evcrawl.api.events import (
    DEFAULT_LIMIT,
    DETAIL_SQL,
    MAX_LIMIT,
    MAX_RADIUS_KM,
    PRICES_SQL,
    STATUS_SQL,
    InvalidSearch,
    Online,
    Search,
    Sort,
    list_sql,
    next_cursor,
)
from evcrawl.api.responses import LIST_MAX_AGE, LONG_MAX_AGE, cached, error
from evcrawl.api.routes import get_db
from evcrawl.api.schemas import (
    CategoryRef,
    ErrorResponse,
    EventDetail,
    EventItem,
    EventList,
    OrganizerRef,
    PriceLotItem,
    PriceRange,
    SourceLink,
    VenueRef,
)

router = APIRouter(prefix="/v1")


def _invalid(exc: InvalidSearch) -> RequestValidationError:
    # The app's handler turns this into the 422 envelope with ``field``.
    return RequestValidationError([{"loc": ("query", exc.field), "msg": exc.message}])


def _coord(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _venue(row: dict[str, Any]) -> VenueRef | None:
    if row["venue_id"] is None:
        return None
    return VenueRef(
        id=row["venue_id"],
        name=row["venue_name"],
        address=row["venue_address"],
        city=row["venue_city"],
        uf=row["venue_uf"],
        lat=_coord(row["lat"]),
        lon=_coord(row["lon"]),
    )


def _price(row: dict[str, Any]) -> PriceRange | None:
    if row["price_min"] is None:
        return None
    return PriceRange(
        min=f"{row['price_min']:.2f}",
        max=f"{row['price_max']:.2f}",
        currency=row["currency"],
        is_free=bool(row["is_free"]),
    )


def _common(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "timezone": row["timezone"],
        "is_online": row["is_online"],
        "image_url": row["image_url"],
        "category": CategoryRef(slug=row["category"], name=row["category_name"]),
        "venue": _venue(row),
        "price": _price(row),
        "sources": [SourceLink.model_validate(s) for s in row["sources"]],
    }


@router.get(
    "/events",
    response_model=EventList,
    responses={422: {"model": ErrorResponse, "description": "A parameter is invalid or missing."}},
)
async def list_events(
    request: Request,
    db: Annotated[Reader, Depends(get_db)],
    lat: Annotated[
        float | None, Query(ge=-90, le=90, description="With lon: the user's position.")
    ] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
    radius_km: Annotated[
        float | None, Query(ge=1, le=MAX_RADIUS_KM, description="Needs lat/lon. Default 25.")
    ] = None,
    city: Annotated[str | None, Query(max_length=64, description="A slug from /v1/cities.")] = None,
    from_: Annotated[
        str | None,
        Query(alias="from", max_length=40, description="ISO date or datetime (UTC). Default: now."),
    ] = None,
    to: Annotated[
        str | None,
        Query(
            max_length=40,
            description="ISO date (inclusive) or datetime. Default: from + 30 days; max span 180.",
        ),
    ] = None,
    category: Annotated[
        list[str] | None, Query(description="Slugs from /v1/categories; repeatable.")
    ] = None,
    free: Annotated[
        bool | None, Query(description="true: a free ticket exists; false: none known.")
    ] = None,
    online: Annotated[
        Online | None, Query(description="Default: exclude with lat/lon, otherwise include.")
    ] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="Words of the title, any accents or case.")
    ] = None,
    sort: Annotated[
        Sort | None, Query(description="Default: distance with lat/lon, otherwise starts_at.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(max_length=200, description="next_cursor of the previous page.")
    ] = None,
) -> Response:
    """Upcoming events near a point or in a city, the nearest (or soonest) first.

    Either `lat`+`lon` or `city` is required. A recurring event (the same
    venue and title, sessions at different times) is one row: its next
    session, with `sessions` counting the others in the window.
    """
    try:
        search = Search.build(
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            city=city,
            window_from=from_,
            window_to=to,
            categories=category,
            free=free,
            online=online,
            q=q,
            sort=sort,
            limit=limit,
            cursor=cursor,
        )
    except InvalidSearch as exc:
        raise _invalid(exc) from None
    sql, params = list_sql(search)
    rows = [dict(r) for r in await db.rows(sql, params)]
    page, more = rows[: search.limit], len(rows) > search.limit
    payload = EventList(
        data=[
            EventItem(
                **_common(row),
                distance_m=None if row["distance_m"] is None else int(row["distance_m"]),
                sessions=int(row["sessions"]),
            )
            for row in page
        ],
        next_cursor=next_cursor(search, page[-1]) if more else None,
    )
    return cached(request, payload, max_age=LIST_MAX_AGE, rows=len(page))


@router.get(
    "/events/{event_id}",
    response_model=EventDetail,
    responses={
        404: {"model": ErrorResponse, "description": "No event has ever had this id."},
        410: {
            "model": ErrorResponse,
            "description": "The event is no longer active; `status` says why.",
        },
    },
)
async def get_event(
    request: Request, db: Annotated[Reader, Depends(get_db)], event_id: int
) -> Response:
    """One event with its description, organizer, price lots and every source link."""
    found = await db.rows(DETAIL_SQL, {"id": event_id})
    if not found:
        status = await db.rows(STATUS_SQL, {"id": event_id})
        if not status:
            return error(404, "not_found", f"no event {event_id}")
        return error(410, "gone", "the event is no longer active", status=str(status[0]["status"]))
    row = dict(found[0])
    prices = await db.rows(PRICES_SQL, {"id": event_id})
    payload = EventDetail(
        **_common(row),
        description=row["description"],
        organizer=(
            OrganizerRef(name=row["organizer_name"], url=row["organizer_url"])
            if row["organizer_name"]
            else None
        ),
        price_lots=[
            PriceLotItem(name=p["name"], amount=f"{p['amount']:.2f}", currency=p["currency"])
            for p in prices
        ],
        updated_at=row["updated_at"],
    )
    return cached(request, payload, max_age=LONG_MAX_AGE, rows=1)
