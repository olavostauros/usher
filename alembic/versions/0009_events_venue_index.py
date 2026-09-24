"""api: index events.venue_id so a radius search joins venues to events by index

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-24

``docs/specs/phase-4-api.md`` task 3. ``/v1/events`` with ``lat``/``lon``
filters ``api_events`` with ``ST_DWithin`` on ``venues.location``; the planner
reaches the venues through ``ix_venues_location`` and then needs the events
of each venue. Without an index on ``events.venue_id`` that step is a scan of
``events``; with it, the nearby venues' events are an index lookup, which is
also what the "no seq scan on ``events``" check in ``tests/test_api_events.py``
holds the plan to.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_events_venue_id", "events", ["venue_id"])


def downgrade() -> None:
    op.drop_index("ix_events_venue_id", table_name="events")
