"""``/v1/categories`` and ``/v1/cities``: the lists the PWA's filters offer."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from evcrawl import cities
from evcrawl.api.db import Reader
from evcrawl.api.responses import LONG_MAX_AGE, cached
from evcrawl.api.routes import get_db
from evcrawl.api.schemas import Category, CategoryList, City, CityList

router = APIRouter(prefix="/v1")


@router.get("/categories", response_model=CategoryList)
async def list_categories(request: Request, db: Annotated[Reader, Depends(get_db)]) -> Response:
    """Every category, the busiest first. ``uncategorized`` holds what sources did not classify."""
    rows = await db.rows(
        "select slug, name, events_upcoming from api_categories order by events_upcoming desc, slug"
    )
    payload = CategoryList(data=[Category.model_validate(dict(r)) for r in rows])
    return cached(request, payload, max_age=LONG_MAX_AGE, rows=len(rows))


@router.get("/cities", response_model=CityList)
async def list_cities(request: Request, db: Annotated[Reader, Depends(get_db)]) -> Response:
    """The cities crawled today, in registry order (Greater Vitória first, then by UF).

    Names, UFs and zones come from the registry (``evcrawl.cities``); only the
    counts come from the database, and a city with no events counts 0.
    """
    rows = await db.rows("select slug, events_upcoming from api_city_counts")
    counts = {r["slug"]: int(r["events_upcoming"]) for r in rows}
    payload = CityList(
        data=[
            City(
                slug=c.slug,
                name=c.name,
                uf=c.uf,
                timezone=c.timezone,
                events_upcoming=counts.get(c.slug, 0),
            )
            for c in cities.CITIES
            if c.enabled
        ]
    )
    return cached(request, payload, max_age=LONG_MAX_AGE, rows=len(payload.data))
