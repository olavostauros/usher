"""monitoring: the freshness views show per-city cycles only (ADR 0012's runs)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-12

Before ADR 0012 (2026-09-23) the scheduler crawled each source once, over
every city, as one ``crawl`` run with no ``city``. Since then ``run_cycle``
writes one ``crawl`` run per registry city and the CLI writes ``manual``
runs, so nothing in the code writes a ``crawl`` run without a city any
more. ``v_source_city_last_run`` still grouped by ``(source, city)`` over
every ``crawl`` run, so the last whole-source runs of 2026-09-23 stayed on
the Freshness board as a ``-`` row per source, ``stale`` or ``errors`` at
~7600 % of the SLA, for ever: a cell no crawl can ever turn fresh again.

The view now reads only ``crawl`` runs that carry a city. A source whose
only ``crawl`` runs have no city shows as ``never``, as an enabled source
never crawled always did. Rows are never deleted (AGENTS.md §7): the old
runs stay in ``crawl_runs`` and in ``v_crawl_runs_hourly``. Columns and
grants are unchanged (``create or replace``), so ``v_source_city_freshness``,
which reads this view, needs no change.

``0011`` was the number the SPECIFICATION §8.1 ``sla`` migration had in
mind; that one takes ``0012``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
_F_COUNTS = ", ".join(f"f.{c}" for c in COUNTS)


def _last_run(per_city: bool) -> str:
    """``v_source_city_last_run``: ``0005``'s, or with *per_city* its ``crawl`` runs with a city."""
    runs = " and city is not null" if per_city else ""
    other = " and r.city is not null" if per_city else ""
    return f"""
        create or replace view v_source_city_last_run as
        with pairs as (
            select distinct source, city from crawl_runs where kind = 'crawl'{runs}
            union
            select s.name, null from sources s   -- enabled, never crawled: 'never'
             where s.enabled
               and not exists (select 1 from crawl_runs r
                                where r.source = s.name and r.kind = 'crawl'{other})
        ),
        finished as (
            select distinct on (source, city) *
              from crawl_runs
             where kind = 'crawl'{runs} and finished_at is not null
             order by source, city, finished_at desc
        ),
        open as (
            select source, city, max(started_at) as running_since
              from crawl_runs
             where kind = 'crawl'{runs} and finished_at is null
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
        """


def upgrade() -> None:
    op.execute(_last_run(per_city=True))


def downgrade() -> None:
    op.execute(_last_run(per_city=False))
