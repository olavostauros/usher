"""phase 3: crawl run metadata per city, listing city / removal / fingerprint

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

Everything Phase 3 changes on the core tables, in one migration, so the shared
``db`` service changes once (``docs/specs/phase-3-national.md`` §3). The new
objects of the other two Phase 3 specs — the SQL views and
``scheduler_heartbeat`` of ``phase-3-monitoring.md``, the ``sla`` table of
``phase-3-freshness.md``, the ``grafana_ro`` role — are additive and land in
their own migrations once this one is merged; no core table changes again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRAWL_KIND = ("crawl", "refresh", "manual")

# Counts run_crawl already keeps in its summary but had nowhere to store.
CRAWL_RUN_COUNTS = ("skipped", "created", "merged", "versions", "removed", "review")


def upgrade() -> None:
    op.add_column("crawl_runs", sa.Column("city", sa.String(length=64), nullable=True))
    # add_column does not emit a non-native Enum's CHECK, so it is created by
    # hand under the name SQLAlchemy gives it in the metadata ("crawl_kind").
    op.add_column(
        "crawl_runs",
        sa.Column("kind", sa.String(length=32), server_default="crawl", nullable=False),
    )
    op.create_check_constraint("crawl_kind", "crawl_runs", sa.column("kind").in_(CRAWL_KIND))
    for name in CRAWL_RUN_COUNTS:
        op.add_column(
            "crawl_runs", sa.Column(name, sa.Integer(), server_default="0", nullable=False)
        )
    op.create_index("ix_crawl_runs_source_city", "crawl_runs", ["source", "city"])

    op.add_column("source_listings", sa.Column("city", sa.String(length=64), nullable=True))
    op.add_column(
        "source_listings", sa.Column("listing_fingerprint", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "source_listings", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_source_listings_source_city", "source_listings", ["source_id", "city"])


def downgrade() -> None:
    op.drop_index("ix_source_listings_source_city", table_name="source_listings")
    op.drop_column("source_listings", "removed_at")
    op.drop_column("source_listings", "listing_fingerprint")
    op.drop_column("source_listings", "city")

    op.drop_index("ix_crawl_runs_source_city", table_name="crawl_runs")
    for name in reversed(CRAWL_RUN_COUNTS):
        op.drop_column("crawl_runs", name)
    op.drop_constraint("crawl_kind", "crawl_runs", type_="check")
    op.drop_column("crawl_runs", "kind")
    op.drop_column("crawl_runs", "city")
