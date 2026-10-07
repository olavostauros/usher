"""api: events_upcoming counts what /v1/events lists (series once, the list's window)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-25

ADR 0016. ``0008`` counted every row of ``api_events`` per category and per
city, so a city's ``events_upcoming`` in ``/v1/cities`` disagreed with the
rows ``/v1/events?city=`` returns three ways: a recurring series is one row
there but was one count per session here (Porto Velho: 1407 against 60);
``api_events`` keeps an event for six hours past its end while the list
drops it at its end; and the list stops at 180 days while the count never
did. The PWA shows the count next to the city as "how many I will see".

``api_categories`` and ``api_city_counts`` now count the rows the list
returns with the default ``from`` (now) and the widest ``to`` it accepts
(the UTC date 179 days ahead, inclusive): rows that have not ended by now
(``ends_at``, or ``OPEN_ENDED_HOURS`` after ``starts_at`` without one) and
start before 00:00 UTC of the 180th day, a series (same venue, same title
without its time-of-day prefix) counted once. The predicate, the horizon
and the series prefix are ``evcrawl.api.events``'s own constants, inlined
here; ``tests/test_api_views.py`` checks the live view still carries them,
so changing one there means a new migration. Columns and grants are
unchanged (``create or replace``); ``api_events`` itself is untouched.
"""

from collections.abc import Sequence

from alembic import op

from evcrawl.api.events import MAX_WINDOW, NOT_ENDED_SQL, SERIES_PREFIX, STARTS_BEFORE_SQL

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

assert "'" not in SERIES_PREFIX  # inlined as a literal below

# Today (UTC) plus the widest window, exclusive: what ``to=<today + 179 days>`` covers.
HORIZON = (
    "date_trunc('day', now() at time zone 'utc') at time zone 'utc'"
    f" + interval '{MAX_WINDOW.days} days'"
)
IN_WINDOW = " and ".join(
    (
        STARTS_BEFORE_SQL.replace(":to_", f"({HORIZON})"),
        NOT_ENDED_SQL.replace(":from_", "now()"),
    )
)
# ``events.py`` partitions by (venue_id, series_key), an event without a venue
# alone; the same groups as one text key, for ``count(distinct …)``.
SERIES = (
    "case when venue_id is null then 'e' || id"
    f" else 'v' || venue_id || ':' || regexp_replace(title_normalized, '{SERIES_PREFIX}', '')"
    " end"
)

UPCOMING = f"""
        with upcoming as (
            select category, cities, {SERIES} as series
              from api_events
             where {IN_WINDOW}
        )
"""

VIEWS: tuple[tuple[str, str], ...] = (
    (
        "api_categories",
        f"""
        {UPCOMING}
        select c.slug, c.name, count(distinct u.series)::bigint as events_upcoming
          from categories c
          left join upcoming u on u.category = c.slug
         group by c.slug, c.name
        """,
    ),
    (
        "api_city_counts",
        f"""
        {UPCOMING}
        select city as slug, count(distinct series)::bigint as events_upcoming
          from upcoming, unnest(cities) as city
         group by city
        """,
    ),
)

# 0008's definitions, for the downgrade.
PREVIOUS: tuple[tuple[str, str], ...] = (
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


def upgrade() -> None:
    for name, body in VIEWS:
        # ``op.execute`` wraps a string in ``text()``, where ``:h`` is a bind
        # parameter: the regex's ``(?:`` groups and the ``':'`` separator need
        # escaping.
        op.execute(f"create or replace view {name} as {body}".replace(":", r"\:"))


def downgrade() -> None:
    for name, body in PREVIOUS:
        op.execute(f"create or replace view {name} as {body}")
