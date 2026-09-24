"""Response models: the shapes ``docs/api/README.md`` documents, and ``openapi.json`` carries."""

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="Machine-readable: invalid_parameter, not_found, gone, …")
    message: str
    field: str | None = Field(default=None, description="The parameter at fault, if one is.")


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
