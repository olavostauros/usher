"""SQLAlchemy models: the single source of truth for the schema (AGENTS.md §7).

Every change here ships with an Alembic migration under ``alembic/versions``.
Conventions: all timestamps are ``timestamptz`` in UTC, money is
``numeric(12,2)`` plus a currency, events are never deleted (``status`` and a new
``event_versions`` row instead), and ``venues.location`` is a PostGIS geography
point queried with ``ST_DWithin``.
"""

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from geoalchemy2 import Geography, WKBElement, WKTElement
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# geography(Point, 4326): read back as WKB, written as WKT. The GiST index is
# declared explicitly (see Venue) so ``metadata.create_all`` and the migration
# produce the same index name.
POINT = Geography(geometry_type="POINT", srid=4326, spatial_index=False)
JSON_DOC = JSON().with_variant(JSONB(), "postgresql")


class EventStatus(enum.StrEnum):
    """Lifecycle of an event row (AGENTS.md §7: never delete, mark instead)."""

    SCHEDULED = "scheduled"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    ENDED = "ended"
    REMOVED_FROM_SOURCE = "removed_from_source"


def event_status() -> Enum:
    """``varchar(32)`` + CHECK holding the enum *values* (``scheduled``, not ``SCHEDULED``)."""
    return Enum(
        EventStatus,
        name="event_status",
        native_enum=False,
        length=32,
        create_constraint=True,
        values_callable=lambda members: [m.value for m in members],
    )


class CrawlKind(enum.StrEnum):
    """Why a ``crawl_runs`` row exists; freshness is measured on ``crawl`` rows only."""

    CRAWL = "crawl"  # a scheduled cycle: the run freshness is measured on
    REFRESH = "refresh"  # the hourly re-check of events starting within 48 h
    MANUAL = "manual"  # a human or the release smoke ran the CLI


def crawl_kind() -> Enum:
    """``varchar(32)`` + CHECK holding the :class:`CrawlKind` values."""
    return Enum(
        CrawlKind,
        name="crawl_kind",
        native_enum=False,
        length=32,
        create_constraint=True,
        values_callable=lambda members: [m.value for m in members],
    )


class Base(DeclarativeBase):
    """Declarative base for all evcrawl tables."""


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def _count() -> Mapped[int]:
    return mapped_column(Integer, default=0, server_default="0", nullable=False)


class CrawlRun(Base):
    """One row per crawl run so freshness can be monitored (AGENTS.md §9).

    The crawl unit is the source × city: ``city`` is the registry slug the run
    covered (``None`` = every city the source lists, and every row written
    before migration ``0003``), and ``kind`` separates the cycle freshness is
    measured on from the hourly refresh and from hand-run crawls.
    """

    __tablename__ = "crawl_runs"
    __table_args__ = (Index("ix_crawl_runs_source_city", "source", "city"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    city: Mapped[str | None] = mapped_column(String(64))  # registry slug (evcrawl.cities)
    kind: Mapped[CrawlKind] = mapped_column(
        crawl_kind(), default=CrawlKind.CRAWL, server_default=CrawlKind.CRAWL.value
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered: Mapped[int] = _count()
    fetched: Mapped[int] = _count()
    upserted: Mapped[int] = _count()
    skipped: Mapped[int] = _count()  # listings whose fingerprint was unchanged (task 6)
    created: Mapped[int] = _count()
    merged: Mapped[int] = _count()  # listings linked to an existing event (ADR 0010)
    versions: Mapped[int] = _count()
    removed: Mapped[int] = _count()
    review: Mapped[int] = _count()
    errors: Mapped[int] = _count()
    error_detail: Mapped[str | None] = mapped_column(Text)


class Source(Base):
    """A ticketing platform we crawl; ``name`` matches the adapter's ``name``."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = _created_at()

    listings: Mapped[list["SourceListing"]] = relationship(back_populates="source")


class Category(Base):
    """Internal taxonomy (AGENTS.md §8); ``uncategorized`` is the catch-all."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created_at()


class Organizer(Base):
    """Who runs the event. Only public data: name and page URL, never contacts (§10)."""

    __tablename__ = "organizers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    name_normalized: Mapped[str] = mapped_column(String(255), unique=True)
    url: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Venue(Base):
    """A place events happen. ``address_normalized`` is also the geocode-cache key."""

    __tablename__ = "venues"
    __table_args__ = (
        UniqueConstraint("address_normalized", name="uq_venues_address_normalized"),
        Index("ix_venues_location", "location", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    address_normalized: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(128), index=True)
    state: Mapped[str | None] = mapped_column(String(8))
    postal_code: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2), default="BR", server_default="BR")
    location: Mapped[WKBElement | WKTElement | None] = mapped_column(POINT)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    events: Mapped[list["Event"]] = relationship(back_populates="venue")


class Event(Base):
    """The canonical (merged) event. One row can be backed by several ``source_listings``."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_title_normalized", "title_normalized"),
        Index("ix_events_starts_at", "starts_at"),
        Index("ix_events_status", "status"),
        Index("ix_events_venue_id", "venue_id"),  # 0009: radius searches (Phase 4 task 3)
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    title_normalized: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="America/Sao_Paulo")
    status: Mapped[EventStatus] = mapped_column(event_status(), default=EventStatus.SCHEDULED)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    image_url: Mapped[str | None] = mapped_column(String(2048))
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id"))
    organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    dedupe_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    first_seen_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _updated_at()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    venue: Mapped[Venue | None] = relationship(back_populates="events")
    organizer: Mapped[Organizer | None] = relationship()
    category: Mapped[Category | None] = relationship()
    price_lots: Mapped[list["PriceLot"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    versions: Mapped[list["EventVersion"]] = relationship(
        back_populates="event", order_by="EventVersion.version"
    )
    listings: Mapped[list["SourceListing"]] = relationship(back_populates="event")


class EventVersion(Base):
    """Append-only history: one row per change of an event's normalized content."""

    __tablename__ = "event_versions"
    __table_args__ = (UniqueConstraint("event_id", "version", name="uq_event_versions_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[EventStatus] = mapped_column(event_status())
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSON_DOC)
    created_at: Mapped[datetime] = _created_at()

    event: Mapped[Event] = relationship(back_populates="versions")


class PriceLot(Base):
    """One price tier of an event. Free events have a row with ``amount = 0`` (§8)."""

    __tablename__ = "price_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="BRL", server_default="BRL")
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = _created_at()

    event: Mapped[Event] = relationship(back_populates="price_lots")


class SourceListing(Base):
    """One platform's listing of an event; links it to the canonical row (attribution, §10).

    ``city`` is the registry slug the listing was discovered under, so removal
    is scoped to the city that was actually crawled; ``removed_at`` marks the
    listing gone from its source (the event itself is only
    ``removed_from_source`` once every listing is, ADR 0012) and
    ``listing_fingerprint`` is the last listing item's hash, which lets a crawl
    skip an unchanged event page.
    """

    __tablename__ = "source_listings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_listings_external_id"),
        Index("ix_source_listings_source_city", "source_id", "city"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    city: Mapped[str | None] = mapped_column(String(64))  # registry slug (evcrawl.cities)
    source_url: Mapped[str] = mapped_column(String(2048))
    content_hash: Mapped[str] = mapped_column(String(64))
    listing_fingerprint: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _updated_at()
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_crawl_run_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_runs.id"))

    source: Mapped[Source] = relationship(back_populates="listings")
    event: Mapped[Event] = relationship(back_populates="listings")
    snapshots: Mapped[list["RawSnapshot"]] = relationship(back_populates="listing")


class RawSnapshot(Base):
    """Gzip of the original payload for a listing, deduplicated by its sha256."""

    __tablename__ = "raw_snapshots"
    __table_args__ = (
        UniqueConstraint("source_listing_id", "sha256", name="uq_raw_snapshots_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_listing_id: Mapped[int] = mapped_column(ForeignKey("source_listings.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(64), default="application/json")
    payload_gzip: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[datetime] = _created_at()

    listing: Mapped[SourceListing] = relationship(back_populates="snapshots")


class GeocodeCache(Base):
    """Geocoder results keyed by normalized address (§8). ``resolved = false`` caches a miss."""

    __tablename__ = "geocode_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address_normalized: Mapped[str] = mapped_column(Text, unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="nominatim")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location: Mapped[WKBElement | WKTElement | None] = mapped_column(POINT)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOC)
    created_at: Mapped[datetime] = _created_at()


class SchedulerHeartbeat(Base):
    """The one row (``id = 1``) the scheduler's ``heartbeat`` job upserts every minute.

    "Is the scheduler alive?" is answered from inside the workspace, where
    containers are invisible, by ``v_scheduler``'s ``age_seconds`` on this row
    (``docs/specs/phase-3-monitoring.md`` §3; down = older than 5 min, spec 3).
    """

    __tablename__ = "scheduler_heartbeat"
    __table_args__ = (CheckConstraint("id = 1", name="scheduler_heartbeat_single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default="1")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    git_sha: Mapped[str] = mapped_column(String(64))
    jobs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


CRAWL_RUN_COUNTS: Final = (
    "discovered",
    "fetched",
    "skipped",
    "upserted",
    "created",
    "merged",
    "versions",
    "removed",
    "review",
    "errors",
)

# The SQL views of migrations 0005 and 0006 are the monitoring contract
# (docs/specs/phase-3-monitoring.md §3): ``ops/host/status.py``, the Grafana
# dashboards under ``ops/grafana/dashboards`` and ``evcrawl health`` (spec 3)
# read these and nothing else, so they cannot disagree. Name -> columns, in the
# order the view returns them; the views only read, nothing is materialised.
#
# - ``v_source_city_last_run``: the last finished ``crawl_runs`` row of kind
#   ``crawl`` per source x city (``city`` is null for whole-source runs and for
#   rows older than 0003), with ``running`` when a newer unfinished row exists;
#   an enabled source with no crawl row at all is one row with nulls, so it
#   reads ``never``. Consumers: ``v_source_city_freshness``, spec 3's ``crawl`` SLA.
# - ``v_source_city_freshness``: the above plus ``sla_seconds`` and the
#   ``verdict`` status.py used to compute (``never`` | ``running`` | ``errors``
#   | ``stale`` | ``fresh``, in that precedence). ``sla_seconds`` is the
#   AGENTS.md §9 six hours until spec 3's ``sla`` table replaces the view.
#   Consumers: status.py, the Freshness dashboard, ``evcrawl health``.
# - ``v_crawl_runs_hourly``: per source, per kind, per hour bucket: ``runs``,
#   the ten counts summed, ``mean_seconds``. Consumers: the Crawl runs
#   dashboard, spec 3's ``errors`` SLA.
# - ``v_catalog_by_city``: per venue city + UF (null for events without a
#   venue): active / upcoming-7-day / under-review / removed events, listings,
#   venues without a location. Consumers: the Catalog dashboard.
# - ``v_geocode_health``: one row: venue and cache totals, the 24 h cache hit
#   ratio, and the share of active in-person events whose venue has a
#   location. Consumers: the Geocoder dashboard, spec 3's ``geocode`` SLA.
# - ``v_scheduler``: the heartbeat row with ``age_seconds``. Consumers:
#   status.py, the Freshness dashboard, spec 3's ``heartbeat`` SLA.
#
# Migration 0006 adds the three the dashboards of spec 2 task 4 need and the
# tables-by-city view cannot give (same rules, same role grant):
#
# - ``v_catalog_by_category``: per internal category slug (``uncategorized``
#   for events without one): active / upcoming-7-day / under-review / removed
#   events. Consumers: the Catalog dashboard.
# - ``v_listings_by_source``: per ``sources`` row: listings total / active
#   (no ``removed_at``) / removed, distinct events and active events, distinct
#   cities. Consumers: the Catalog dashboard.
# - ``v_venues_unresolved``: one row per venue without a ``location``: id,
#   name, city, UF, ``first_seen_at`` (``created_at``), active events at it.
#   Names only, never addresses (spec §5). Consumers: the Geocoder dashboard.
VIEWS: Final[dict[str, tuple[str, ...]]] = {
    "v_source_city_last_run": (
        "source",
        "city",
        "run_id",
        "started_at",
        "finished_at",
        "age_seconds",
        *CRAWL_RUN_COUNTS,
        "error_line",
        "running",
        "running_since",
    ),
    "v_source_city_freshness": (
        "source",
        "city",
        "run_id",
        "started_at",
        "finished_at",
        "age_seconds",
        *CRAWL_RUN_COUNTS,
        "error_line",
        "running",
        "running_since",
        "sla_seconds",
        "verdict",
    ),
    "v_crawl_runs_hourly": ("source", "kind", "hour", "runs", *CRAWL_RUN_COUNTS, "mean_seconds"),
    "v_catalog_by_city": (
        "city",
        "uf",
        "events_active",
        "events_upcoming_7d",
        "events_under_review",
        "events_removed",
        "listings",
        "venues_without_location",
    ),
    "v_geocode_health": (
        "venues_total",
        "venues_with_location",
        "venues_without_location",
        "cache_rows",
        "cache_resolved",
        "cache_negative",
        "hit_ratio_24h",
        "active_events",
        "active_events_located",
        "active_events_located_ratio",
    ),
    "v_scheduler": ("id", "started_at", "beat_at", "git_sha", "jobs", "age_seconds"),
    "v_catalog_by_category": (
        "category",
        "events_active",
        "events_upcoming_7d",
        "events_under_review",
        "events_removed",
    ),
    "v_listings_by_source": (
        "source",
        "enabled",
        "listings",
        "listings_active",
        "listings_removed",
        "events",
        "events_active",
        "cities",
    ),
    "v_venues_unresolved": ("venue_id", "name", "city", "uf", "first_seen_at", "events_active"),
}

# The views of migration 0008 are the events API's contract
# (docs/specs/phase-4-api.md §3): the API reads these as ``api_ro`` and
# nothing else, and the PWA sees their columns through ``docs/api/``. Renaming
# or removing a column is a breaking change and ships as ``/v2``.
#
# - ``api_events``: one row per active event (``scheduled`` | ``updated``)
#   ending, or without an end starting, no more than six hours ago; under
#   ``dedupe_review`` too. ``category`` is the slug (``uncategorized`` when
#   none), ``location`` the venue's geography point for ``ST_DWithin`` with
#   ``lat``/``lon`` beside it, the prices the range over its lots
#   (``is_free`` when any lot is 0, null when it has none), ``cities`` the
#   registry slugs and ``sources`` the ``[{source, url}]`` of its live
#   listings, oldest first.
# - ``api_event_prices``: the price lots of the events in ``api_events``.
# - ``api_event_status``: every event's id and status, ``ended`` for an
#   active one past the six hours; 404 vs 410 on the detail endpoint.
# - ``api_categories``: every category with its count in ``api_events``.
# - ``api_city_counts``: per registry slug, its count in ``api_events``;
#   names, UFs and zones come from ``evcrawl.cities``.
API_VIEWS: Final[dict[str, tuple[str, ...]]] = {
    "api_events": (
        "id",
        "title",
        "title_normalized",
        "description",
        "starts_at",
        "ends_at",
        "timezone",
        "is_online",
        "image_url",
        "category",
        "category_name",
        "venue_id",
        "venue_name",
        "venue_address",
        "venue_city",
        "venue_uf",
        "location",
        "lat",
        "lon",
        "organizer_name",
        "organizer_url",
        "price_min",
        "price_max",
        "currency",
        "is_free",
        "cities",
        "sources",
        "updated_at",
    ),
    "api_event_prices": ("event_id", "name", "amount", "currency", "position"),
    "api_event_status": ("id", "status"),
    "api_categories": ("slug", "name", "events_upcoming"),
    "api_city_counts": ("slug", "events_upcoming"),
}
