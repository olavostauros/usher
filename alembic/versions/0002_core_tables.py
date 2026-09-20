"""core tables: sources, categories, organizers, venues, events, event_versions,
price_lots, source_listings, raw_snapshots, geocode_cache

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""

from collections.abc import Sequence
from datetime import datetime

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_STATUS = ("scheduled", "updated", "cancelled", "ended", "removed_from_source")


def _point() -> geoalchemy2.Geography:
    # The GiST index is created explicitly below (spatial_index=False here).
    return geoalchemy2.Geography(geometry_type="POINT", srid=4326, spatial_index=False)


def _status() -> sa.Enum:
    return sa.Enum(
        *EVENT_STATUS, name="event_status", native_enum=False, length=32, create_constraint=True
    )


def _json() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _timestamp(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        _timestamp("created_at"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        _timestamp("created_at"),
    )
    op.create_table(
        "organizers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_normalized", sa.String(length=255), nullable=False, unique=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
    )
    op.create_table(
        "venues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("address_normalized", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=8), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=2), server_default="BR", nullable=False),
        sa.Column("location", _point(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("address_normalized", name="uq_venues_address_normalized"),
    )
    op.create_index("ix_venues_city", "venues", ["city"])
    op.create_index("ix_venues_location", "venues", ["location"], postgresql_using="gist")
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("status", _status(), nullable=False),
        sa.Column("is_online", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("image_url", sa.String(length=2048), nullable=True),
        sa.Column("venue_id", sa.Integer(), sa.ForeignKey("venues.id"), nullable=True),
        sa.Column("organizer_id", sa.Integer(), sa.ForeignKey("organizers.id"), nullable=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("dedupe_review", sa.Boolean(), server_default="false", nullable=False),
        _timestamp("first_seen_at"),
        _timestamp("last_seen_at"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
    )
    op.create_index("ix_events_title_normalized", "events", ["title_normalized"])
    op.create_index("ix_events_starts_at", "events", ["starts_at"])
    op.create_index("ix_events_status", "events", ["status"])
    op.create_table(
        "event_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _status(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content", _json(), nullable=False),
        _timestamp("created_at"),
        sa.UniqueConstraint("event_id", "version", name="uq_event_versions_version"),
    )
    op.create_index("ix_event_versions_event_id", "event_versions", ["event_id"])
    op.create_table(
        "price_lots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="BRL", nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        _timestamp("created_at"),
    )
    op.create_index("ix_price_lots_event_id", "price_lots", ["event_id"])
    op.create_table(
        "source_listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        _timestamp("first_seen_at"),
        _timestamp("last_seen_at"),
        sa.Column("last_crawl_run_id", sa.Integer(), sa.ForeignKey("crawl_runs.id"), nullable=True),
        sa.UniqueConstraint("source_id", "external_id", name="uq_source_listings_external_id"),
    )
    op.create_index("ix_source_listings_event_id", "source_listings", ["event_id"])
    op.create_table(
        "raw_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_listing_id",
            sa.Integer(),
            sa.ForeignKey("source_listings.id"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("payload_gzip", sa.LargeBinary(), nullable=False),
        _timestamp("fetched_at"),
        sa.UniqueConstraint("source_listing_id", "sha256", name="uq_raw_snapshots_sha256"),
    )
    op.create_table(
        "geocode_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address_normalized", sa.Text(), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resolved", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("location", _point(), nullable=True),
        sa.Column("response", _json(), nullable=True),
        _timestamp("created_at"),
    )


def downgrade() -> None:
    op.drop_table("geocode_cache")
    op.drop_table("raw_snapshots")
    op.drop_index("ix_source_listings_event_id", table_name="source_listings")
    op.drop_table("source_listings")
    op.drop_index("ix_price_lots_event_id", table_name="price_lots")
    op.drop_table("price_lots")
    op.drop_index("ix_event_versions_event_id", table_name="event_versions")
    op.drop_table("event_versions")
    op.drop_index("ix_events_status", table_name="events")
    op.drop_index("ix_events_starts_at", table_name="events")
    op.drop_index("ix_events_title_normalized", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_venues_location", table_name="venues")
    op.drop_index("ix_venues_city", table_name="venues")
    op.drop_table("venues")
    op.drop_table("organizers")
    op.drop_table("categories")
    op.drop_table("sources")
