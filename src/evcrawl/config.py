"""Runtime configuration. Every value comes from the environment (AGENTS.md §8)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings, read once from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://evcrawl:evcrawl@localhost:5432/evcrawl",
        description="SQLAlchemy URL for the PostgreSQL + PostGIS database.",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    evcrawl_contact_email: str = Field(default="contato@example.org")
    evcrawl_user_agent: str = Field(
        default="EventoLivreBot/0.1 (+mailto:contato@example.org)",
        description="Descriptive User-Agent identifying Evento Livre (AGENTS.md §5).",
    )
    nominatim_url: str = Field(default="https://nominatim.openstreetmap.org")
    evcrawl_geocode_rps: float = Field(
        default=1.0,
        gt=0,
        description="Requests per second at the geocoder; clamped to 1 on the public instance.",
    )
    log_level: str = Field(default="INFO")
    evcrawl_git_sha: str = Field(default="unknown", description="Set by the image build.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
