"""The events API's views (migration 0008) and the ``api_ro`` role that reads them.

``docs/SPECIFICATION.md`` §7: the columns are the ``API_VIEWS``
contract, ``api_ro`` reads every ``api_*`` view and nothing else, and the
radius query through ``api_events`` reaches ``ix_venues_location``.
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from evcrawl.api.events import MAX_WINDOW, OPEN_ENDED_HOURS, SERIES_PREFIX
from evcrawl.db.models import (
    API_VIEWS,
    VIEWS,
    Category,
    Event,
    EventStatus,
    PriceLot,
    Source,
    SourceListing,
    Venue,
)
from evcrawl.db.roles import (
    API,
    API_ROLE,
    GRAFANA,
    READ_ONLY_ROLES,
    role_can_login,
)

VITORIA = (-20.3155, -40.3128)  # lat, lon


def test_api_views_are_named_apart_from_the_monitoring_ones() -> None:
    assert all(name.startswith("api_") for name in API_VIEWS)
    assert not set(API_VIEWS) & set(VIEWS)


def test_api_ro_is_a_bounded_read_only_role_with_its_own_password() -> None:
    assert READ_ONLY_ROLES == (GRAFANA, API)
    assert API.name == API_ROLE == "api_ro"
    assert API.password_env == "EVCRAWL_API_DB_PASSWORD"
    settings = dict(API.settings)
    assert settings["default_transaction_read_only"] == "on"
    assert settings["statement_timeout"] == "3s"
    assert API.connection_limit > 0


def test_every_api_view_returns_exactly_its_contract_columns(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn:
        for view, columns in API_VIEWS.items():
            result = conn.execute(text(f"select * from {view} limit 0"))
            assert tuple(result.keys()) == columns, view


def _event(title: str, starts_in: timedelta, **kwargs: object) -> Event:
    return Event(
        title=title,
        title_normalized=title.lower(),
        starts_at=datetime.now(UTC) + starts_in,
        timezone="America/Sao_Paulo",
        content_hash="0" * 64,
        **kwargs,
    )


def _listing(source: Source, event: Event, n: int, city: str, **kwargs: object) -> SourceListing:
    return SourceListing(
        source=source,
        event=event,
        external_id=f"{source.name}-{n}",
        city=city,
        source_url=f"https://{source.name}.example.org/e/{n}",
        content_hash="0" * 64,
        **kwargs,
    )


def _seed(engine: Engine) -> dict[str, int]:
    """Events around Vitória and one online: live, ended, cancelled, removed, under review."""
    now = datetime.now(UTC)
    with Session(engine) as session:
        sympla = Source(name="sympla", base_url="https://sympla.example.org")
        zig = Source(name="zig", base_url="https://zig.example.org")
        music = Category(slug="music", name="Música")
        uncategorized = Category(slug="uncategorized", name="Sem categoria")
        venue = Venue(
            name="Teatro",
            address="Rua 1, Centro, Vitória - ES",
            address_normalized="rua 1 centro vitoria es",
            city="Vitória",
            state="ES",
            location=f"SRID=4326;POINT({VITORIA[1]} {VITORIA[0]})",
        )
        unlocated = Venue(name="Bar", address_normalized="bar serra es", city="Serra", state="ES")
        live = _event(
            "Jazz",
            timedelta(days=2),
            status=EventStatus.SCHEDULED,
            venue=venue,
            category=music,
            price_lots=[
                PriceLot(name="Gratuito", amount=Decimal("0.00"), position=0),
                PriceLot(name="Pista", amount=Decimal("80.00"), position=1),
            ],
        )
        online = _event(
            "Live",
            timedelta(days=1),
            status=EventStatus.UPDATED,
            is_online=True,
            dedupe_review=True,
        )
        running = _event(
            "Feira",
            -timedelta(hours=3),
            status=EventStatus.SCHEDULED,
            ends_at=now + timedelta(hours=1),
            venue=unlocated,
            category=music,
        )
        past = _event("Passado", -timedelta(days=2), status=EventStatus.SCHEDULED, venue=venue)
        cancelled = _event(
            "Cancelado",
            timedelta(days=3),
            status=EventStatus.CANCELLED,
            venue=venue,
            price_lots=[PriceLot(name="Inteira", amount=Decimal("50.00"))],
        )
        removed = _event("Sumiu", timedelta(days=3), status=EventStatus.REMOVED_FROM_SOURCE)
        # ADR 0016: in api_events, but not in a count: ended an hour ago (six
        # hours of grace) and past the widest window; counted once: two sessions.
        ended = _event(
            "Acabou",
            -timedelta(hours=5),
            status=EventStatus.SCHEDULED,
            ends_at=now - timedelta(hours=1),
            venue=venue,
        )
        far = _event("Longe", MAX_WINDOW + timedelta(days=1), status=EventStatus.SCHEDULED)
        sessions = [
            _event(f"{h}h | Visita Guiada", timedelta(days=4, hours=h), venue=venue)
            for h in (9, 11)
        ]
        session.add_all(
            [
                _listing(sympla, ended, 9, "vitoria"),
                _listing(sympla, far, 10, "vitoria"),
                _listing(sympla, sessions[0], 11, "vitoria"),
                _listing(sympla, sessions[1], 12, "vitoria"),
                uncategorized,
                _listing(sympla, live, 1, "vitoria", first_seen_at=now - timedelta(days=2)),
                _listing(zig, live, 2, "vila-velha", first_seen_at=now - timedelta(days=1)),
                _listing(sympla, live, 3, "serra", removed_at=now),
                _listing(sympla, online, 4, "sao-paulo"),
                _listing(zig, running, 5, "vitoria"),
                _listing(sympla, past, 6, "vitoria"),
                _listing(sympla, cancelled, 7, "vitoria"),
                _listing(zig, removed, 8, "vitoria", removed_at=now),
            ]
        )
        session.commit()
        return {
            e.title: e.id
            for e in (live, online, running, past, cancelled, removed, ended, far, *sessions)
        }


def test_api_views_serve_active_events_and_nothing_about_the_rest(scratch_engine: Engine) -> None:
    ids = _seed(scratch_engine)
    with scratch_engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(text("select * from api_events")).all()}
        assert set(rows) == {
            ids["Jazz"],
            ids["Live"],
            ids["Feira"],
            ids["Acabou"],
            ids["Longe"],
            ids["9h | Visita Guiada"],
            ids["11h | Visita Guiada"],
        }

        jazz = rows[ids["Jazz"]]
        assert (jazz.category, jazz.category_name) == ("music", "Música")
        assert (jazz.venue_name, jazz.venue_city, jazz.venue_uf) == ("Teatro", "Vitória", "ES")
        assert jazz.lat == pytest.approx(VITORIA[0]) and jazz.lon == pytest.approx(VITORIA[1])
        assert (jazz.price_min, jazz.price_max) == (Decimal("0.00"), Decimal("80.00"))
        assert jazz.currency == "BRL" and jazz.is_free is True
        assert jazz.cities == ["vila-velha", "vitoria"]  # the removed serra listing is gone
        assert jazz.sources == [
            {"source": "sympla", "url": "https://sympla.example.org/e/1"},
            {"source": "zig", "url": "https://zig.example.org/e/2"},
        ]

        live = rows[ids["Live"]]
        assert live.is_online and live.venue_id is None and live.lat is None
        assert (live.category, live.category_name) == ("uncategorized", "Sem categoria")
        assert live.price_min is None and live.is_free is None and live.currency is None
        assert live.cities == ["sao-paulo"]

        feira = rows[ids["Feira"]]
        assert feira.venue_city == "Serra" and feira.location is None and feira.lat is None

        prices = conn.execute(
            text("select event_id, name, amount from api_event_prices order by position")
        ).all()
        assert [(p.event_id, p.name, p.amount) for p in prices] == [
            (ids["Jazz"], "Gratuito", Decimal("0.00")),
            (ids["Jazz"], "Pista", Decimal("80.00")),
        ]

        status = dict(conn.execute(text("select id, status from api_event_status")).all())
        assert status == {
            ids["Jazz"]: "scheduled",
            ids["Live"]: "updated",
            ids["Feira"]: "scheduled",
            ids["Passado"]: "ended",
            ids["Cancelado"]: "cancelled",
            ids["Sumiu"]: "removed_from_source",
            ids["Acabou"]: "scheduled",
            ids["Longe"]: "scheduled",
            ids["9h | Visita Guiada"]: "scheduled",
            ids["11h | Visita Guiada"]: "scheduled",
        }

        categories = dict(
            conn.execute(text("select slug, events_upcoming from api_categories")).all()
        )
        assert categories == {"music": 2, "uncategorized": 2}  # Live and the guided tour
        cities = dict(conn.execute(text("select slug, events_upcoming from api_city_counts")).all())
        assert cities == {"vitoria": 3, "vila-velha": 1, "sao-paulo": 1}


def test_the_count_views_carry_the_lists_window_and_series_rule(scratch_engine: Engine) -> None:
    """Migration 0010 inlines ``events.py``'s constants: a change there needs a new migration."""
    with scratch_engine.connect() as conn:
        for view in ("api_categories", "api_city_counts"):
            body = conn.execute(text("select pg_get_viewdef(:v)"), {"v": view}).scalar_one()
            assert SERIES_PREFIX in body, view
            assert f"'{MAX_WINDOW.days} days'::interval" in body, view
            assert f"starts_at + '{OPEN_ENDED_HOURS:02d}:00:00'::interval" in body, view
            assert "now()" in body and "AT TIME ZONE 'utc'" in body, view


def _api_ro_or_skip(conn: Connection) -> None:
    """``api_ro`` is cluster-wide and created by a superuser migrate (0008).

    A member's scratch migrate as ``team`` cannot create it; until the shared
    database is released with 0008 the role is missing, which is a skip in a
    member's clone and a failure in CI, where the superuser migrated first.
    """
    if role_can_login(conn, API_ROLE) is None and not os.environ.get(
        "EVCRAWL_REQUIRE_TEST_DATABASE"
    ):
        pytest.skip("api_ro missing: the shared db is not released with 0008 yet")


def test_api_ro_reads_every_api_view_and_nothing_else(scratch_engine: Engine) -> None:
    _seed(scratch_engine)
    with scratch_engine.connect() as conn:
        _api_ro_or_skip(conn)
        conn.execute(text(f"set role {API_ROLE}"))
        for view in API_VIEWS:
            assert conn.execute(text(f"select * from {view}")).all(), view
        for denied in (
            "select 1 from events limit 1",
            "select 1 from raw_snapshots limit 1",
            "select 1 from v_scheduler limit 1",
            "select 1 from crawl_runs limit 1",
            "delete from source_listings",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                conn.execute(text(denied))
            conn.rollback()
            conn.execute(text(f"set role {API_ROLE}"))
        conn.execute(text("reset role"))


def test_api_ro_is_bounded_and_read_only(scratch_engine: Engine) -> None:
    """Spec §3: read-only sessions, a 3 s statement timeout, a connection limit.

    ``set role`` does not load a role's settings, so this reads the catalog,
    as ``test_grafana_ro_is_bounded`` does.
    """
    with scratch_engine.connect() as conn:
        _api_ro_or_skip(conn)
        config, limit = conn.execute(
            text("select rolconfig, rolconnlimit from pg_roles where rolname = :r"),
            {"r": API_ROLE},
        ).one()
    if not config and not os.environ.get("EVCRAWL_REQUIRE_TEST_DATABASE"):
        pytest.skip("api_ro unbounded: only a superuser migrate bounds it")
    assert sorted(config) == sorted(f"{name}={value}" for name, value in API.settings)
    assert limit == API.connection_limit


def test_radius_query_through_api_events_uses_the_venue_gist_index(scratch_engine: Engine) -> None:
    """With sequential scans priced out, the plan must reach ``ix_venues_location``.

    A seeded scratch database is too small for the planner to prefer the
    index on its own; forbidding seq scans proves the view lets the
    ``ST_DWithin`` filter through to ``venues`` instead of hiding it behind
    the aggregates.
    """
    _seed(scratch_engine)
    point = f"st_setsrid(st_makepoint({VITORIA[1]}, {VITORIA[0]}), 4326)::geography"
    with scratch_engine.connect() as conn:
        conn.execute(text("set local enable_seqscan = off"))
        plan = "\n".join(
            conn.execute(
                text(
                    "explain select id, st_distance(location, "
                    f"{point}) as distance_m from api_events "
                    f"where st_dwithin(location, {point}, 25000) "
                    "order by distance_m, id limit 20"
                )
            ).scalars()
        )
    assert "ix_venues_location" in plan, plan
