"""monitoring: the six SQL views, scheduler_heartbeat and the grafana_ro role

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23

``docs/specs/phase-3-monitoring.md`` §3, task 1. The views are the contract
that ``ops/host/status.py``, Grafana and (spec 3) ``evcrawl health`` all read,
so they cannot disagree; ``evcrawl.db.models.VIEWS`` lists their columns.

- ``scheduler_heartbeat``: one row (``id = 1``) the scheduler's ``heartbeat``
  job upserts every minute (task 2); ``v_scheduler`` exposes its age.
- ``v_source_city_freshness`` joins the ``sla`` table of
  ``phase-3-freshness.md`` once that exists. Until then the SLA is the
  AGENTS.md §9 six hours as a constant (``SLA_SECONDS``); spec 3's migration
  replaces the view (``create or replace``) to read ``sla`` instead.
- ``grafana_ro`` is the database's first non-superuser role: ``SELECT`` on the
  views and nothing else. Its password comes from ``GRAFANA_DB_PASSWORD`` in
  the migrating process's environment (the host ``.env`` through
  ``docker compose run --rm evcrawl migrate``); when the variable is unset,
  as in CI and on a scratch database, the role is created ``NOLOGIN`` so the
  grants and the tests still exercise it. Alembic never re-runs this file, so
  ``evcrawl migrate`` re-applies the password after every upgrade
  (``evcrawl.db.roles``): a later release with the variable set turns login
  on, and changing the variable rotates it. Roles are cluster-wide: the
  scratch database and the shared one see the same ``grafana_ro``, so this
  migration never overwrites a password that already works, and the
  downgrade drops the role only when no other database still grants it.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

from evcrawl.db.roles import GRAFANA_ROLE, PASSWORD_ENV, ensure_grafana_role

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

ROLE = GRAFANA_ROLE
SLA_SECONDS = 6 * 3600  # AGENTS.md §9, until spec 3's ``sla`` table exists

COUNTS = (
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
_COUNTS = ", ".join(COUNTS)
_F_COUNTS = ", ".join(f"f.{c}" for c in COUNTS)
_SUM_COUNTS = ", ".join(f"coalesce(sum({c}), 0)::bigint as {c}" for c in COUNTS)

# Ordered: each view may only read tables and the views created before it.
VIEWS: tuple[tuple[str, str], ...] = (
    (
        "v_source_city_last_run",
        f"""
        with pairs as (
            select distinct source, city from crawl_runs where kind = 'crawl'
            union
            select s.name, null from sources s   -- enabled, never crawled: 'never'
             where s.enabled
               and not exists (select 1 from crawl_runs r
                                where r.source = s.name and r.kind = 'crawl')
        ),
        finished as (
            select distinct on (source, city) *
              from crawl_runs
             where kind = 'crawl' and finished_at is not null
             order by source, city, finished_at desc
        ),
        open as (
            select source, city, max(started_at) as running_since
              from crawl_runs
             where kind = 'crawl' and finished_at is null
             group by source, city
        )
        select p.source,
               p.city,
               f.id as run_id,
               f.started_at,
               f.finished_at,
               extract(epoch from now() - f.finished_at)::bigint as age_seconds,
               {_F_COUNTS},
               split_part(f.error_detail, E'\\n', 1) as error_line,
               (o.running_since is not null
                and (f.started_at is null or o.running_since > f.started_at)) as running,
               o.running_since
          from pairs p
          left join finished f on f.source = p.source and f.city is not distinct from p.city
          left join open o on o.source = p.source and o.city is not distinct from p.city
        """,
    ),
    (
        "v_source_city_freshness",
        f"""
        select r.*,
               {SLA_SECONDS}::integer as sla_seconds,
               case
                 when r.finished_at is null and not r.running then 'never'
                 when r.running then 'running'
                 when r.errors > 0 then 'errors'
                 when r.age_seconds > {SLA_SECONDS} then 'stale'
                 else 'fresh'
               end as verdict
          from v_source_city_last_run r
        """,
    ),
    (
        "v_crawl_runs_hourly",
        f"""
        select source,
               kind,
               date_trunc('hour', started_at) as hour,
               count(*)::bigint as runs,
               {_SUM_COUNTS},
               avg(extract(epoch from finished_at - started_at))
                   filter (where finished_at is not null) as mean_seconds
          from crawl_runs
         group by source, kind, date_trunc('hour', started_at)
        """,
    ),
    (
        "v_catalog_by_city",
        """
        with located as (
            select e.id, e.status, e.starts_at, e.dedupe_review, v.city, v.state as uf
              from events e
              left join venues v on v.id = e.venue_id
        ),
        by_event as (
            select city, uf,
                   count(*) filter (where status in ('scheduled', 'updated'))::bigint
                       as events_active,
                   count(*) filter (where status in ('scheduled', 'updated')
                                    and starts_at >= now()
                                    and starts_at < now() + interval '7 days')::bigint
                       as events_upcoming_7d,
                   count(*) filter (where dedupe_review)::bigint as events_under_review,
                   count(*) filter (where status = 'removed_from_source')::bigint
                       as events_removed
              from located
             group by city, uf
        ),
        by_listing as (
            select l.city, l.uf, count(*)::bigint as listings
              from source_listings sl
              join located l on l.id = sl.event_id
             group by l.city, l.uf
        ),
        by_venue as (
            select city, state as uf,
                   count(*) filter (where location is null)::bigint as venues_without_location
              from venues
             group by city, state
        )
        select e.city,
               e.uf,
               e.events_active,
               e.events_upcoming_7d,
               e.events_under_review,
               e.events_removed,
               coalesce(l.listings, 0) as listings,
               coalesce(v.venues_without_location, 0) as venues_without_location
          from by_event e
          left join by_listing l on l.city is not distinct from e.city
                                and l.uf is not distinct from e.uf
          left join by_venue v on v.city is not distinct from e.city
                              and v.uf is not distinct from e.uf
        """,
    ),
    (
        "v_geocode_health",
        """
        select (select count(*) from venues)::bigint as venues_total,
               (select count(*) from venues where location is not null)::bigint
                   as venues_with_location,
               (select count(*) from venues where location is null)::bigint
                   as venues_without_location,
               (select count(*) from geocode_cache)::bigint as cache_rows,
               (select count(*) from geocode_cache where resolved)::bigint as cache_resolved,
               (select count(*) from geocode_cache where not resolved)::bigint as cache_negative,
               (select count(*) filter (where resolved)::float / nullif(count(*), 0)
                  from geocode_cache
                 where created_at >= now() - interval '24 hours') as hit_ratio_24h,
               a.active_events,
               a.active_events_located,
               a.active_events_located::float / nullif(a.active_events, 0)
                   as active_events_located_ratio
          from (select count(*)::bigint as active_events,
                       count(*) filter (where v.location is not null)::bigint
                           as active_events_located
                  from events e
                  left join venues v on v.id = e.venue_id
                 where e.status in ('scheduled', 'updated') and not e.is_online) a
        """,
    ),
    (
        "v_scheduler",
        """
        select id,
               started_at,
               beat_at,
               git_sha,
               jobs,
               extract(epoch from now() - beat_at)::bigint as age_seconds
          from scheduler_heartbeat
         where id = 1
        """,
    ),
)


def _role_exists(conn: sa.Connection) -> bool:
    row = conn.execute(sa.text("select 1 from pg_roles where rolname = :r"), {"r": ROLE}).scalar()
    return row is not None


def upgrade() -> None:
    op.create_table(
        "scheduler_heartbeat",
        sa.Column("id", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("jobs", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name="scheduler_heartbeat_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, body in VIEWS:
        op.execute(f"create view {name} as {body}")

    conn = op.get_bind()
    log.info("%s: %s", ROLE, ensure_grafana_role(conn, os.environ.get(PASSWORD_ENV), rotate=False))
    op.execute(f"grant connect on database {_current_database(conn)} to {ROLE}")
    op.execute(f"grant usage on schema public to {ROLE}")
    op.execute(f"grant select on {', '.join(name for name, _ in VIEWS)} to {ROLE}")


def _current_database(conn: sa.Connection) -> str:
    return conn.execute(sa.text("select quote_ident(current_database())")).scalar_one()


def downgrade() -> None:
    conn = op.get_bind()
    if _role_exists(conn):
        op.execute(f"revoke all on {', '.join(name for name, _ in VIEWS)} from {ROLE}")
        op.execute(f"revoke usage on schema public from {ROLE}")
        op.execute(f"revoke connect on database {_current_database(conn)} from {ROLE}")
    for name, _ in reversed(VIEWS):
        op.execute(f"drop view {name}")
    op.drop_table("scheduler_heartbeat")
    if _role_exists(conn):
        # Cluster-wide: another database (the shared db, when this is scratch)
        # may still grant the role; then it stays and only this db lets go.
        savepoint = conn.begin_nested()
        try:
            conn.execute(sa.text(f"drop role {ROLE}"))
            savepoint.commit()
        except DBAPIError as exc:
            savepoint.rollback()
            if getattr(exc.orig, "sqlstate", None) != "2BP01":  # dependent objects exist
                raise
            log.warning("%s is still granted elsewhere in the cluster; left in place", ROLE)
