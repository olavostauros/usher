"""Schema checks: offline against the SQLAlchemy metadata, and (when a scratch
database is configured) the migrations themselves."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2 import Geography
from sqlalchemy import Engine, Enum, Numeric, UniqueConstraint, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from evcrawl.db.models import (
    VIEWS,
    Base,
    Category,
    CrawlKind,
    CrawlRun,
    Event,
    EventStatus,
    EventVersion,
    GeocodeCache,
    Organizer,
    PriceLot,
    RawSnapshot,
    SchedulerHeartbeat,
    Source,
    SourceListing,
    Venue,
)

COUNT_COLUMNS = (
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

CORE_TABLES = {
    "crawl_runs",
    "sources",
    "categories",
    "organizers",
    "venues",
    "events",
    "event_versions",
    "price_lots",
    "source_listings",
    "raw_snapshots",
    "geocode_cache",
    "scheduler_heartbeat",
}


def unique_columns(table: str) -> set[tuple[str, ...]]:
    t = Base.metadata.tables[table]
    out = {
        tuple(c.name for c in uc.columns)
        for uc in t.constraints
        if isinstance(uc, UniqueConstraint)
    }
    out |= {(c.name,) for c in t.columns if c.unique}
    return out


def test_metadata_declares_the_core_tables() -> None:
    assert set(Base.metadata.tables) == CORE_TABLES


def test_every_timestamp_is_timestamptz() -> None:
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.type.__class__.__name__ == "DateTime":
                assert getattr(col.type, "timezone", False), f"{table.name}.{col.name}"


def test_venue_location_is_geography_point_with_gist_index() -> None:
    col = Venue.__table__.c.location
    assert isinstance(col.type, Geography)
    assert col.type.geometry_type == "POINT"
    assert col.type.srid == 4326
    gist = [i for i in Venue.__table__.indexes if i.name == "ix_venues_location"]
    assert gist and gist[0].dialect_options["postgresql"]["using"] == "gist"


def test_money_is_numeric_12_2_with_currency() -> None:
    amount = PriceLot.__table__.c.amount.type
    assert isinstance(amount, Numeric) and (amount.precision, amount.scale) == (12, 2)
    assert PriceLot.__table__.c.currency.server_default is not None


def test_unique_keys() -> None:
    assert ("source_id", "external_id") in unique_columns("source_listings")
    assert ("address_normalized",) in unique_columns("geocode_cache")
    assert ("address_normalized",) in unique_columns("venues")
    assert ("event_id", "version") in unique_columns("event_versions")
    assert ("source_listing_id", "sha256") in unique_columns("raw_snapshots")
    assert ("name",) in unique_columns("sources")
    assert ("slug",) in unique_columns("categories")


def test_event_status_values_are_lowercase_strings() -> None:
    assert [s.value for s in EventStatus] == [
        "scheduled",
        "updated",
        "cancelled",
        "ended",
        "removed_from_source",
    ]
    status_type = Event.__table__.c.status.type
    assert isinstance(status_type, Enum)
    assert status_type.enums == [s.value for s in EventStatus]
    assert status_type.native_enum is False


def test_source_listing_keeps_attribution_url() -> None:
    assert not SourceListing.__table__.c.source_url.nullable


def test_crawl_run_records_city_kind_and_every_summary_count() -> None:
    cols = CrawlRun.__table__.c
    assert cols.city.nullable  # a run over every city of a source has no one city
    assert not cols.kind.nullable
    kind_type = cols.kind.type
    assert isinstance(kind_type, Enum)
    assert kind_type.enums == [k.value for k in CrawlKind]
    assert kind_type.native_enum is False
    for count in COUNT_COLUMNS:
        assert not cols[count].nullable, count
        # 0004: every count defaults server-side, so a row can be written
        # without naming them (the six 0003 added always could).
        assert cols[count].server_default is not None, count
    index = [i for i in CrawlRun.__table__.indexes if i.name == "ix_crawl_runs_source_city"]
    assert index and [c.name for c in index[0].columns] == ["source", "city"]


def test_crawl_kind_values_are_lowercase_strings() -> None:
    assert [k.value for k in CrawlKind] == ["crawl", "refresh", "manual"]


def test_source_listing_carries_city_removal_and_fingerprint() -> None:
    cols = SourceListing.__table__.c
    assert cols.city.nullable and cols.removed_at.nullable
    assert cols.listing_fingerprint.nullable and cols.listing_fingerprint.type.length == 64
    index = [
        i for i in SourceListing.__table__.indexes if i.name == "ix_source_listings_source_city"
    ]
    assert index and [c.name for c in index[0].columns] == ["source_id", "city"]


# --- against the scratch database (skipped unless EVCRAWL_TEST_DATABASE_URL is set)


def test_migration_head_matches_models(scratch_engine: Engine) -> None:
    names = set(inspect(scratch_engine).get_table_names())
    assert CORE_TABLES <= names
    with scratch_engine.connect() as conn:
        version = conn.execute(text("select version_num from alembic_version")).scalar_one()
        assert version == "0005"
        indexes = conn.execute(
            text("select indexdef from pg_indexes where indexname = 'ix_venues_location'")
        ).scalar_one()
        assert "USING gist" in indexes
        coltype = conn.execute(
            text(
                "select format_type(atttypid, atttypmod) from pg_attribute "
                "where attrelid = 'venues'::regclass and attname = 'location'"
            )
        ).scalar_one()
        assert coltype == "geography(Point,4326)"


def test_crawl_run_row_takes_every_default_and_checks_kind(scratch_engine: Engine) -> None:
    with Session(scratch_engine) as session:
        run = CrawlRun(source="test")
        session.add(run)
        session.commit()
        # Existing rows migrate with kind 'crawl' and zero counts; the server
        # defaults are what makes the ALTER safe on the shared db.
        assert run.city is None and run.kind is CrawlKind.CRAWL
        assert (run.skipped, run.created, run.merged, run.versions, run.removed, run.review) == (
            0,
            0,
            0,
            0,
            0,
            0,
        )
        # A row written without the ORM takes every default: kind 'crawl' and
        # zero counts, which is what the rows already in the shared db become.
        # Naming nothing but source works only because 0004 levelled up the
        # four counts 0001 created without a server default.
        session.execute(text("insert into crawl_runs (source) values ('raw')"))
        session.commit()
        counts = ", ".join(COUNT_COLUMNS)
        row = session.execute(
            text(f"select kind, {counts} from crawl_runs where source = 'raw'")
        ).one()
        assert row == ("crawl", *(0 for _ in COUNT_COLUMNS))

        with pytest.raises(Exception, match="crawl_kind"):
            session.execute(text("insert into crawl_runs (source, kind) values ('x', 'nope')"))
        session.rollback()


def test_round_trip_through_the_orm(scratch_engine: Engine) -> None:
    with Session(scratch_engine) as session:
        source = Source(name="test", base_url="https://example.org")
        category = Category(slug="uncategorized", name="Sem categoria")
        organizer = Organizer(name="Org", name_normalized="org")
        venue = Venue(
            name="Praça",
            address="Praça, 1, Vitória - ES",
            address_normalized="praca 1 vitoria es",
            city="Vitória",
            state="ES",
            location="SRID=4326;POINT(-40.3050745 -20.3119792)",
        )
        event = Event(
            title="Show",
            title_normalized="show",
            starts_at=datetime.now(UTC),
            timezone="America/Sao_Paulo",
            status=EventStatus.SCHEDULED,
            venue=venue,
            organizer=organizer,
            category=category,
            content_hash="0" * 64,
            price_lots=[PriceLot(name="Gratuito", amount=Decimal("0.00"))],
            versions=[
                EventVersion(
                    version=1, status=EventStatus.SCHEDULED, content_hash="0" * 64, content={}
                )
            ],
        )
        listing = SourceListing(
            source=source,
            event=event,
            external_id="1",
            city="vitoria",
            source_url="https://example.org/e/1",
            content_hash="0" * 64,
            listing_fingerprint="a" * 64,
            snapshots=[RawSnapshot(sha256="0" * 64, payload_gzip=b"\x1f\x8b")],
        )
        session.add_all([listing, GeocodeCache(address_normalized="praca 1 vitoria es")])
        session.commit()

        stored = session.get(Event, event.id)
        assert stored is not None
        assert stored.status is EventStatus.SCHEDULED
        assert stored.listings[0].city == "vitoria"
        assert stored.listings[0].listing_fingerprint == "a" * 64
        assert stored.listings[0].removed_at is None
        assert stored.price_lots[0].amount == Decimal("0.00")
        assert stored.price_lots[0].currency == "BRL"
        raw_status = session.execute(text("select status from events")).scalar_one()
        assert raw_status == "scheduled"
        within = session.execute(
            text(
                "select count(*) from venues where ST_DWithin(location, "
                "ST_GeogFromText('SRID=4326;POINT(-40.305 -20.312)'), 300)"
            )
        ).scalar_one()
        assert within == 1
        with pytest.raises(Exception, match="uq_source_listings_external_id"):
            session.add(
                SourceListing(
                    source=source,
                    event=event,
                    external_id="1",
                    source_url="https://example.org/e/1-again",
                    content_hash="1" * 64,
                )
            )
            session.flush()
        session.rollback()


# --- monitoring (migration 0005): the views are the contract, grafana_ro reads them only


def test_scheduler_heartbeat_is_one_row() -> None:
    cols = SchedulerHeartbeat.__table__.c
    assert cols.id.server_default is not None and not cols.beat_at.nullable
    checks = [c.name for c in SchedulerHeartbeat.__table__.constraints if c.name]
    assert "scheduler_heartbeat_single_row" in checks


def test_every_view_returns_exactly_its_contract_columns(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn:
        for view, columns in VIEWS.items():
            result = conn.execute(text(f"select * from {view} limit 0"))
            assert tuple(result.keys()) == columns, view


def crawl_run(
    session: Session,
    source: str,
    city: str | None,
    *,
    kind: str = "crawl",
    finished_ago: timedelta | None = timedelta(hours=1),
    started_ago: timedelta | None = None,
    errors: int = 0,
    error_detail: str | None = None,
    discovered: int = 10,
) -> None:
    now = datetime.now(UTC)
    finished = None if finished_ago is None else now - finished_ago
    started = now - (started_ago or ((finished_ago or timedelta()) + timedelta(minutes=5)))
    session.add(
        CrawlRun(
            source=source,
            city=city,
            kind=CrawlKind(kind),
            started_at=started,
            finished_at=finished,
            discovered=discovered,
            fetched=discovered,
            errors=errors,
            error_detail=error_detail,
        )
    )


def test_freshness_view_carries_status_py_verdicts(scratch_engine: Engine) -> None:
    with Session(scratch_engine) as session:
        session.add_all(
            [
                Source(name="never", base_url="https://n.example.org"),
                Source(name="off", base_url="https://o.example.org", enabled=False),
            ]
        )
        crawl_run(session, "sympla", "vitoria")  # fresh
        crawl_run(session, "sympla", "vitoria", finished_ago=timedelta(hours=9))  # older: ignored
        crawl_run(session, "sympla", "serra", finished_ago=timedelta(hours=7))  # stale
        crawl_run(session, "zig", "vitoria", errors=2, error_detail="boom\nsecond line")  # errors
        crawl_run(session, "blueticket", "vitoria", errors=1)  # ... but a newer run is open:
        crawl_run(
            session, "blueticket", "vitoria", finished_ago=None, started_ago=timedelta(minutes=2)
        )
        crawl_run(
            session, "uticket", "vitoria", finished_ago=None, started_ago=timedelta(minutes=1)
        )
        crawl_run(session, "ticketbolt", "vitoria", kind="manual")  # the smoke: not a cycle
        crawl_run(session, "ticketbolt", None, kind="refresh")  # hourly: not a cycle
        session.commit()
        rows = session.execute(
            text(
                "select source, city, verdict, errors, error_line, running, sla_seconds "
                "from v_source_city_freshness order by source, city"
            )
        ).all()
    by_key = {(r.source, r.city): r for r in rows}
    assert set(by_key) == {
        ("sympla", "vitoria"),
        ("sympla", "serra"),
        ("zig", "vitoria"),
        ("blueticket", "vitoria"),
        ("uticket", "vitoria"),
        ("never", None),
    }
    assert by_key[("sympla", "vitoria")].verdict == "fresh"
    assert by_key[("sympla", "serra")].verdict == "stale"
    assert by_key[("zig", "vitoria")].verdict == "errors"
    assert by_key[("zig", "vitoria")].error_line == "boom"
    assert by_key[("blueticket", "vitoria")].verdict == "running"  # running wins over errors
    assert by_key[("blueticket", "vitoria")].errors == 1
    assert by_key[("uticket", "vitoria")].verdict == "running"
    assert by_key[("never", None)].verdict == "never"
    assert {r.sla_seconds for r in rows} == {6 * 3600}  # AGENTS.md §9 until spec 3's sla table


def test_hourly_view_sums_runs_per_source_kind_and_hour(scratch_engine: Engine) -> None:
    with Session(scratch_engine) as session:
        crawl_run(session, "zig", "vitoria", finished_ago=timedelta(minutes=10), discovered=3)
        crawl_run(session, "zig", "serra", finished_ago=timedelta(minutes=12), discovered=4)
        crawl_run(session, "zig", None, kind="refresh", finished_ago=timedelta(minutes=11))
        session.commit()
        rows = session.execute(
            text(
                "select source, kind, runs, discovered, fetched, mean_seconds "
                "from v_crawl_runs_hourly order by kind"
            )
        ).all()
    assert [(r.source, r.kind, r.runs, r.discovered, r.fetched) for r in rows] == [
        ("zig", "crawl", 2, 7, 7),
        ("zig", "refresh", 1, 10, 10),
    ]
    assert all(abs(r.mean_seconds - 300) < 1 for r in rows)


def test_catalog_geocode_and_scheduler_views(scratch_engine: Engine) -> None:
    now = datetime.now(UTC)
    with Session(scratch_engine) as session:
        source = Source(name="test", base_url="https://example.org")
        located = Venue(
            name="Praça",
            address_normalized="praca 1 vitoria es",
            city="Vitória",
            state="ES",
            location="SRID=4326;POINT(-40.3050745 -20.3119792)",
        )
        unlocated = Venue(
            name="Bar", address_normalized="bar 2 manaus am", city="Manaus", state="AM"
        )

        def event(
            title: str,
            venue: Venue,
            status: EventStatus,
            *,
            days_ahead: int = 2,
            review: bool = False,
        ) -> Event:
            return Event(
                title=title,
                title_normalized=title.lower(),
                starts_at=now + timedelta(days=days_ahead),
                status=status,
                venue=venue,
                content_hash="0" * 64,
                dedupe_review=review,
            )

        soon = event("Show", located, EventStatus.SCHEDULED)
        later = event("Feira", located, EventStatus.UPDATED, days_ahead=30)
        gone = event("Antigo", unlocated, EventStatus.REMOVED_FROM_SOURCE, review=True)
        for i, ev in enumerate((soon, later, gone)):
            session.add(
                SourceListing(
                    source=source,
                    event=ev,
                    external_id=str(i),
                    source_url=f"https://example.org/e/{i}",
                    content_hash="0" * 64,
                )
            )
        session.add(
            SourceListing(  # a second listing of the merged event
                source=source, event=soon, external_id="9", source_url="x", content_hash="1" * 64
            )
        )
        session.add_all(
            [
                GeocodeCache(address_normalized="praca 1 vitoria es", resolved=True),
                GeocodeCache(address_normalized="bar 2 manaus am", resolved=False),
            ]
        )
        session.add(
            SchedulerHeartbeat(
                started_at=now - timedelta(hours=1),
                beat_at=now - timedelta(seconds=40),
                git_sha="abc1234",
                jobs=11,
            )
        )
        session.commit()

        catalog = {
            (r.city, r.uf): r
            for r in session.execute(text("select * from v_catalog_by_city")).all()
        }
        assert set(catalog) == {("Vitória", "ES"), ("Manaus", "AM")}
        vix = catalog[("Vitória", "ES")]
        assert (vix.events_active, vix.events_upcoming_7d, vix.events_under_review) == (2, 1, 0)
        assert (vix.events_removed, vix.listings, vix.venues_without_location) == (0, 3, 0)
        man = catalog[("Manaus", "AM")]
        assert (man.events_active, man.events_under_review, man.events_removed) == (0, 1, 1)
        assert (man.listings, man.venues_without_location) == (1, 1)

        geo = session.execute(text("select * from v_geocode_health")).one()
        assert (geo.venues_total, geo.venues_with_location, geo.venues_without_location) == (
            2,
            1,
            1,
        )
        assert (geo.cache_rows, geo.cache_resolved, geo.cache_negative) == (2, 1, 1)
        assert geo.hit_ratio_24h == 0.5
        assert (geo.active_events, geo.active_events_located) == (2, 2)
        assert geo.active_events_located_ratio == 1.0

        beat = session.execute(text("select * from v_scheduler")).one()
        assert (beat.git_sha, beat.jobs) == ("abc1234", 11)
        assert 40 <= beat.age_seconds < 60

        with pytest.raises(DBAPIError, match="scheduler_heartbeat_single_row"):
            session.execute(
                text(
                    "insert into scheduler_heartbeat (id, started_at, beat_at, git_sha) "
                    "values (2, now(), now(), 'x')"
                )
            )
        session.rollback()


def test_grafana_ro_can_select_the_views_and_nothing_else(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn:
        conn.execute(text("set role grafana_ro"))
        for view in VIEWS:
            conn.execute(text(f"select * from {view} limit 1")).all()
        for denied in (
            "select 1 from events limit 1",
            "select 1 from raw_snapshots limit 1",
            "select 1 from crawl_runs limit 1",
            "insert into scheduler_heartbeat (started_at, beat_at, git_sha) "
            "values (now(), now(), 'x')",
            "delete from source_listings",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                conn.execute(text(denied))
            conn.rollback()
            conn.execute(text("set role grafana_ro"))
        conn.execute(text("reset role"))
