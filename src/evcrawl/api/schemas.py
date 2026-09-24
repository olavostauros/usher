"""Response models: the shapes ``docs/api/README.md`` documents, and ``openapi.json`` carries."""

from datetime import datetime

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="Machine-readable: invalid_parameter, not_found, gone, …")
    message: str
    field: str | None = Field(default=None, description="The parameter at fault, if one is.")
    status: str | None = Field(
        default=None,
        description="With code `gone`: cancelled, ended or removed_from_source.",
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Health(BaseModel):
    ok: bool
    git_sha: str
    db: str = Field(description='"ok", or "unavailable" when the database does not answer.')


class Category(BaseModel):
    slug: str = Field(examples=["music"])
    name: str = Field(examples=["Música"])
    events_upcoming: int = Field(description="Active events that have not ended.")


class CategoryList(BaseModel):
    data: list[Category]


class City(BaseModel):
    slug: str = Field(examples=["vitoria"])
    name: str = Field(examples=["Vitória"])
    uf: str = Field(examples=["ES"])
    timezone: str = Field(examples=["America/Sao_Paulo"], description="IANA zone.")
    events_upcoming: int = Field(description="Active events that have not ended.")


class CityList(BaseModel):
    data: list[City]


class CategoryRef(BaseModel):
    slug: str = Field(examples=["music"])
    name: str = Field(examples=["Música"])


class VenueRef(BaseModel):
    id: int
    name: str | None
    address: str | None
    city: str | None
    uf: str | None
    lat: float | None = Field(
        description="WGS 84, 6 decimals; null when the venue is not geocoded."
    )
    lon: float | None


class PriceRange(BaseModel):
    min: str = Field(examples=["0.00"], description="Decimal string; never parse as a float.")
    max: str = Field(examples=["80.00"])
    currency: str = Field(examples=["BRL"])
    is_free: bool = Field(description="At least one ticket costs 0.00.")


class SourceLink(BaseModel):
    source: str = Field(examples=["sympla"])
    url: str = Field(description="The listing on the platform that sells the tickets.")


class EventItem(BaseModel):
    """One row of ``/v1/events``: no description, no price lots (get those from the detail)."""

    id: int
    title: str
    starts_at: datetime
    ends_at: datetime | None
    timezone: str = Field(
        examples=["America/Sao_Paulo"], description="IANA zone for local display."
    )
    is_online: bool
    image_url: str | None
    category: CategoryRef
    venue: VenueRef | None = Field(description="null for online events.")
    distance_m: int | None = Field(
        description="Metres from lat/lon; null without lat/lon, and for online events."
    )
    price: PriceRange | None = Field(description="null when the source published no prices.")
    sources: list[SourceLink]
    sessions: int = Field(
        ge=1,
        description=(
            "Sessions of a recurring event this row stands for, within the search window; "
            "1 for an ordinary event. The row is the next session."
        ),
    )


class EventList(BaseModel):
    data: list[EventItem]
    next_cursor: str | None = Field(
        description="Pass as `cursor` for the next page; null at the end."
    )


class OrganizerRef(BaseModel):
    name: str
    url: str | None


class PriceLotItem(BaseModel):
    name: str
    amount: str = Field(examples=["45.00"], description="Decimal string.")
    currency: str


class EventDetail(BaseModel):
    """``/v1/events/{id}``: the list item plus description, organizer, price lots, updated_at."""

    id: int
    title: str
    description: str | None
    starts_at: datetime
    ends_at: datetime | None
    timezone: str
    is_online: bool
    image_url: str | None
    category: CategoryRef
    venue: VenueRef | None
    organizer: OrganizerRef | None
    price: PriceRange | None
    price_lots: list[PriceLotItem] = Field(description="In the source's order.")
    sources: list[SourceLink] = Field(description="Every platform listing it; the oldest first.")
    updated_at: datetime
