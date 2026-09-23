"""monitoring: three more views the Grafana dashboards need

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23

``docs/specs/phase-3-monitoring.md`` §3 lists panels that the six views of
``0005`` cannot feed: the Catalog dashboard's "events by category" and
"listings per source", and the Geocoder dashboard's "unresolved venues
table (name, city, first seen)". A panel may only read a view (the contract
``evcrawl.db.models.VIEWS``; ``grafana_ro`` sees nothing else), so task 4
adds these three, additive like the others, and grants them to the role.
Venue names are public event data (spec §5); addresses, descriptions and
raw snapshots stay out.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from evcrawl.db.roles import GRAFANA_ROLE

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE = "in ('scheduled', 'updated')"

VIEWS: tuple[tuple[str, str], ...] = (
    (
        "v_catalog_by_category",
        f"""
        select coalesce(c.slug, 'uncategorized') as category,
               count(*) filter (where e.status {ACTIVE})::bigint as events_active,
               count(*) filter (where e.status {ACTIVE}
                                and e.starts_at >= now()
                                and e.starts_at < now() + interval '7 days')::bigint
                   as events_upcoming_7d,
               count(*) filter (where e.dedupe_review)::bigint as events_under_review,
               count(*) filter (where e.status = 'removed_from_source')::bigint
                   as events_removed
          from events e
          left join categories c on c.id = e.category_id
         group by coalesce(c.slug, 'uncategorized')
        """,
    ),
    (
        "v_listings_by_source",
        f"""
        select s.name as source,
               s.enabled,
               count(sl.id)::bigint as listings,
               count(sl.id) filter (where sl.removed_at is null)::bigint as listings_active,
               count(sl.id) filter (where sl.removed_at is not null)::bigint
                   as listings_removed,
               count(distinct sl.event_id)::bigint as events,
               count(distinct sl.event_id) filter (where e.status {ACTIVE})::bigint
                   as events_active,
               count(distinct sl.city)::bigint as cities
          from sources s
          left join source_listings sl on sl.source_id = s.id
          left join events e on e.id = sl.event_id
         group by s.id, s.name, s.enabled
        """,
    ),
    (
        "v_venues_unresolved",
        f"""
        select v.id as venue_id,
               v.name,
               v.city,
               v.state as uf,
               v.created_at as first_seen_at,
               count(e.id) filter (where e.status {ACTIVE})::bigint as events_active
          from venues v
          left join events e on e.venue_id = v.id
         where v.location is null
         group by v.id
        """,
    ),
)

_NAMES = ", ".join(name for name, _ in VIEWS)


def _role_exists(conn: sa.Connection) -> bool:
    row = conn.execute(
        sa.text("select 1 from pg_roles where rolname = :r"), {"r": GRAFANA_ROLE}
    ).scalar()
    return row is not None


def upgrade() -> None:
    for name, body in VIEWS:
        op.execute(f"create view {name} as {body}")
    op.execute(f"grant select on {_NAMES} to {GRAFANA_ROLE}")


def downgrade() -> None:
    if _role_exists(op.get_bind()):
        op.execute(f"revoke all on {_NAMES} from {GRAFANA_ROLE}")
    for name, _ in reversed(VIEWS):
        op.execute(f"drop view {name}")
