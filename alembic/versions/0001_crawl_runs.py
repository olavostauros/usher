"""crawl_runs

Revision ID: 0001
Revises:
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered", sa.Integer(), nullable=False),
        sa.Column("fetched", sa.Integer(), nullable=False),
        sa.Column("upserted", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_crawl_runs_source", "crawl_runs", ["source"])


def downgrade() -> None:
    op.drop_index("ix_crawl_runs_source", table_name="crawl_runs")
    op.drop_table("crawl_runs")
