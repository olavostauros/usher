"""api: the five api_* views the events API reads, and the api_ro role

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24

``docs/specs/phase-4-api.md`` §3, task 1. The views are the contract the
events API (and so the Usher PWA) reads, the way ``0005`` made the monitoring
views Grafana's; ``evcrawl.db.models.API_VIEWS`` lists their columns.

- ``api_events``: one row per active, not-yet-ended event (``scheduled`` or
  ``updated``, ending or, without an end, starting no more than six hours
  ago), with its venue, point, organizer, category, price range and the
  cities and links of its live listings. Events under ``dedupe_review`` are
  in: they are real events, possibly listed twice. The aggregates are
  lateral subqueries per event, so a radius filter on ``location`` reaches
  ``ix_venues_location`` through the view.
- ``api_event_prices``: the price lots of those events only.
- ``api_event_status``: ``id, status`` for every event, so the detail
  endpoint tells 404 from 410. An active event past the six hours reads
  ``ended`` here, since ``api_events`` no longer has it and its row keeps
  ``scheduled`` until the crawl notices.
- ``api_categories``: every category with its count in ``api_events``.
- ``api_city_counts``: registry slug (``source_listings.city``) with its
  count in ``api_events``. Names, UFs and zones come from
  ``evcrawl.cities`` in the app, so the registry stays the one source.

``api_ro`` is ``grafana_ro``'s twin (``evcrawl.db.roles.API``): ``SELECT``
on these views and nothing else, read-only sessions with a 3 s statement
timeout, its password from ``EVCRAWL_API_DB_PASSWORD``, applied by
``evcrawl migrate`` after every upgrade. Roles are cluster-wide: the release
migrate creates it as the superuser; a member's scratch migrate (as ``team``)
finds it, or, before that release, cannot create it and leaves the grants
out, and the tests that need it skip. The downgrade drops the role only when
no other database still grants it.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

from evcrawl.db.roles import API, ensure_read_only_role, role_can_login

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

ROLE = API.name
ACTIVE = "e.status in ('scheduled', 'updated')"
NOT_ENDED = "coalesce(e.ends_at, e.starts_at) >= now() - interval '6 hours'"

# Ordered: each view may only read tables and the views created before it.
VIEWS: tuple[tuple[str, str], ...] = (
    (
        "api_events",
        f"""
        select e.id,
               e.title,
               e.title_normalized,
               e.description,
               e.starts_at,
               e.ends_at,
               e.timezone,
               e.is_online,
               e.image_url,
               coalesce(c.slug, 'uncategorized') as category,
               coalesce(c.name, 'Sem categoria') as category_name,
               v.id as venue_id,
               v.name as venue_name,
               v.address as venue_address,
               v.city as venue_city,
               v.state as venue_uf,
               v.location,
               st_y(v.location::geometry) as lat,
               st_x(v.location::geometry) as lon,
               o.name as organizer_name,
               o.url as organizer_url,
               p.price_min,
               p.price_max,
               p.currency,
               p.is_free,
               coalesce(l.cities, '{{}}'::text[]) as cities,
               coalesce(l.sources, '[]'::jsonb) as sources,
               e.updated_at
          from events e
          left join venues v on v.id = e.venue_id
          left join organizers o on o.id = e.organizer_id
          left join categories c on c.id = e.category_id
          left join lateral (
              select min(pl.amount) as price_min,
                     max(pl.amount) as price_max,
                     min(pl.currency) as currency,
                     bool_or(pl.amount = 0) as is_free
                from price_lots pl
               where pl.event_id = e.id
          ) p on true
          left join lateral (
              select array_agg(distinct sl.city::text order by sl.city::text)
                         filter (where sl.city is not null) as cities,
                     jsonb_agg(jsonb_build_object('source', s.name, 'url', sl.source_url)
                               order by sl.first_seen_at, sl.id) as sources
                from source_listings sl
                join sources s on s.id = sl.source_id
               where sl.event_id = e.id and sl.removed_at is null
          ) l on true
         where {ACTIVE}
           and {NOT_ENDED}
        """,
    ),
    (
        "api_event_prices",
        f"""
        select pl.event_id, pl.name, pl.amount, pl.currency, pl.position
          from price_lots pl
          join events e on e.id = pl.event_id
         where {ACTIVE}
           and {NOT_ENDED}
        """,
    ),
    (
        "api_event_status",
        f"""
        select e.id,
               case when {ACTIVE} and not ({NOT_ENDED}) then 'ended'
                    else e.status::text
               end as status
          from events e
        """,
    ),
    (
        "api_categories",
        """
        select c.slug, c.name, count(a.id)::bigint as events_upcoming
          from categories c
          left join api_events a on a.category = c.slug
         group by c.slug, c.name
        """,
    ),
    (
        "api_city_counts",
        """
        select city as slug, count(*)::bigint as events_upcoming
          from api_events, unnest(cities) as city
         group by city
        """,
    ),
)
_NAMES = ", ".join(name for name, _ in VIEWS)


def _current_database(conn: sa.Connection) -> str:
    return conn.execute(sa.text("select quote_ident(current_database())")).scalar_one()


def upgrade() -> None:
    for name, body in VIEWS:
        op.execute(f"create view {name} as {body}")

    conn = op.get_bind()
    status = ensure_read_only_role(conn, API, os.environ.get(API.password_env), rotate=False)
    log.info("%s: %s", ROLE, status)
    if status == "missing":
        return
    op.execute(f"grant connect on database {_current_database(conn)} to {ROLE}")
    op.execute(f"grant usage on schema public to {ROLE}")
    op.execute(f"grant select on {_NAMES} to {ROLE}")


def downgrade() -> None:
    conn = op.get_bind()
    exists = role_can_login(conn, ROLE) is not None
    if exists:
        op.execute(f"revoke all on {_NAMES} from {ROLE}")
        op.execute(f"revoke usage on schema public from {ROLE}")
        op.execute(f"revoke connect on database {_current_database(conn)} from {ROLE}")
    for name, _ in reversed(VIEWS):
        op.execute(f"drop view {name}")
    if exists:
        # Cluster-wide, as 0005's grafana_ro: another database may still grant
        # it, and a member migrating as `team` may not drop roles at all.
        savepoint = conn.begin_nested()
        try:
            conn.execute(sa.text(f"drop role {ROLE}"))
            savepoint.commit()
        except DBAPIError as exc:
            savepoint.rollback()
            # 2BP01: dependent objects exist; 42501: not a superuser or CREATEROLE.
            if getattr(exc.orig, "sqlstate", None) not in ("2BP01", "42501"):
                raise
            log.warning("%s is still granted elsewhere in the cluster; left in place", ROLE)
