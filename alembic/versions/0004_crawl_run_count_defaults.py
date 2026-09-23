"""crawl_runs: server defaults for the four counts 0001 created without one

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23

``discovered``, ``fetched``, ``upserted`` and ``errors`` have been NOT NULL with
no server default since ``0001``: only the ORM's ``default=0`` fills them, so a
plain ``insert into crawl_runs (source) values (...)`` fails. The six counts
``0003`` added do have one, and the split is a trap for anything that writes a
run row without the ORM (a SQL fixture, a backfill, a view's test data). This
levels the four up; nothing about how the pipeline writes rows changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_COUNTS = ("discovered", "fetched", "upserted", "errors")


def upgrade() -> None:
    for name in LEGACY_COUNTS:
        op.alter_column(
            "crawl_runs",
            name,
            server_default="0",
            existing_type=sa.Integer(),
            existing_nullable=False,
        )


def downgrade() -> None:
    for name in LEGACY_COUNTS:
        op.alter_column(
            "crawl_runs",
            name,
            server_default=None,
            existing_type=sa.Integer(),
            existing_nullable=False,
        )
