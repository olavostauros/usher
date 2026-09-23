"""ops/host/status.py reads the monitoring views and rolls them up (phase 3 monitoring, task 2).

The pure parts (parsing psql rows, the per-source roll-up, the ``--hours``
override, the scheduler verdict) run everywhere. The SQL strings run through a
real ``psql`` against the scratch database, so a renamed view column fails here
and not on the host (skipped without ``EVCRAWL_TEST_DATABASE_URL``).
"""

import importlib.util
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Engine, text

from evcrawl.db.models import VIEWS
from tests.conftest import SCRATCH_URL

STATUS_PY = Path(__file__).resolve().parents[1] / "ops" / "host" / "status.py"


def load_status() -> ModuleType:
    spec = importlib.util.spec_from_file_location("status", STATUS_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["status"] = module  # dataclasses resolve the module's annotations through here
    spec.loader.exec_module(module)
    return module


status = load_status()


def city_row(
    source: str,
    city: str,
    verdict: str,
    age: int = 600,
    errors: int = 0,
    running: bool = False,
) -> list[str]:
    """A FRESHNESS_SQL row as psql prints it: strings, t/f, '' for null."""
    started = "" if verdict == "never" else "2026-09-23 12:00"
    return [
        source,
        city,
        started,
        str(-1 if verdict == "never" else age),
        "t" if running else "f",
        "2026-09-23 12:30" if running else "",
        "10",
        "8",
        "2",
        "7",
        "1",
        str(errors),
        "boom" if errors else "",
        str(6 * 3600),
        verdict,
    ]


# --- pure


def test_city_statuses_parse_psql_rows_as_the_view_returns_them() -> None:
    [c] = status.city_statuses([city_row("sympla", "vitoria", "fresh", age=90)], None)
    assert (c.source, c.city, c.verdict, c.age_seconds, c.sla_seconds) == (
        "sympla",
        "vitoria",
        "fresh",
        90,
        21600,
    )
    assert (c.discovered, c.fetched, c.skipped, c.upserted, c.removed, c.errors) == (
        10,
        8,
        2,
        7,
        1,
        0,
    )
    assert not c.running and c.running_since == "" and c.error_line == ""


def test_hours_overrides_the_views_sla_only_for_fresh_and_stale() -> None:
    rows = [
        city_row("a", "x", "fresh", age=2 * 3600),  # fresh at 6 h, stale at 1 h
        city_row("a", "y", "stale", age=7 * 3600),  # stale at 6 h, fresh at 8 h
        city_row("a", "z", "errors", age=10, errors=1),  # never depends on the window
        city_row("b", "", "never"),
        city_row("c", "x", "running", running=True),
    ]
    assert [c.verdict for c in status.city_statuses(rows, None)] == [
        "fresh",
        "stale",
        "errors",
        "never",
        "running",
    ]
    assert [c.verdict for c in status.city_statuses(rows, 1)] == [
        "stale",
        "stale",
        "errors",
        "never",
        "running",
    ]
    assert [c.verdict for c in status.city_statuses(rows, 8)] == [
        "fresh",
        "fresh",
        "errors",
        "never",
        "running",
    ]
    assert all(c.sla_seconds == 8 * 3600 for c in status.city_statuses(rows, 8))


def test_rollup_reports_the_worst_city_and_the_newest_run() -> None:
    cities = status.city_statuses(
        [
            city_row("sympla", "vitoria", "fresh", age=3000),
            city_row("sympla", "sao-paulo", "stale", age=30000),
            city_row("sympla", "manaus", "fresh", age=100),
            city_row("zig", "vitoria", "errors", age=50, errors=2),
            city_row("zig", "recife", "stale", age=40000),
            city_row("uticket", "", "never"),
            city_row("ticketbolt", "vitoria", "running", running=True),
            city_row("ticketbolt", "belem", "fresh", age=500),
        ],
        None,
    )
    by = {s.source: s for s in status.rollup(cities)}
    assert by["sympla"].verdict == "stale" and by["sympla"].cities == 3
    assert by["sympla"].age_seconds == 100 and by["sympla"].by_verdict == {"fresh": 2, "stale": 1}
    assert by["sympla"].discovered == 30 and by["sympla"].errors == 0
    assert by["zig"].verdict == "errors" and by["zig"].errors == 2  # errors beat stale
    assert by["uticket"].verdict == "never" and by["uticket"].age_seconds == -1
    assert by["uticket"].last_started == ""
    assert by["ticketbolt"].verdict == "running"  # running beats fresh
    assert by["ticketbolt"].age_seconds == 500  # the running city has no finished run


def test_scheduler_status_is_the_heartbeat_age_against_five_minutes() -> None:
    up = status.scheduler_status(
        [["2026-09-23 12:00", "2026-09-23 12:41:07", "abc", "11", "42"]], ""
    )
    assert up.up and up.age_seconds == 42 and up.jobs == 11 and up.git_sha == "abc"
    down = status.scheduler_status(
        [["2026-09-23 12:00", "2026-09-23 12:41:07", "abc", "11", "301"]], ""
    )
    assert not down.up
    never = status.scheduler_status([], "running (healthy)")
    assert not never.up and never.age_seconds == -1 and never.container == "running (healthy)"
    assert status.HEARTBEAT_DOWN_SECONDS == 300
    assert "up (heartbeat 42s, 11 jobs, build abc)" in status.fmt_scheduler(up)
    assert (
        status.fmt_scheduler(never)
        == "scheduler: down (container running (healthy), no heartbeat yet)"
    )


def test_fmt_age() -> None:
    assert [status.fmt_age(s) for s in (-1, 5, 61, 3700, 90000)] == [
        "-",
        "5s",
        "1m",
        "1h01",
        "1d01h",
    ]


# --- against the scratch database, through psql like the script does


def scratch_dsn() -> str:
    if not shutil.which("psql"):
        pytest.skip("psql is not installed here (it is in the dev image)")
    assert SCRATCH_URL
    return SCRATCH_URL.replace("postgresql+psycopg://", "postgresql://")


def test_every_sql_string_runs_on_the_views(scratch_engine: Engine) -> None:
    dsn = scratch_dsn()
    assert status.psql("inside", status.FRESHNESS_SQL, dsn) == []
    assert status.psql("inside", status.SCHEDULER_SQL, dsn) == []
    [totals] = status.psql("inside", status.TOTALS_SQL, dsn)
    assert len(totals) == len(status.TOTAL_KEYS) and all(t == "0" for t in totals)
    for view in ("v_source_city_freshness", "v_scheduler", "v_catalog_by_city", "v_geocode_health"):
        assert view in VIEWS and view in status.__doc__


def test_build_report_inside_the_workspace(
    scratch_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = scratch_dsn()
    now = datetime.now(UTC)
    with scratch_engine.begin() as conn:
        conn.execute(
            text(
                "insert into sources (name, base_url, enabled) values "
                "('sympla', 'https://s', true), ('zig', 'https://z', true), "
                "('uticket', 'https://u', true), ('blueticket', 'https://b', false)"
            )
        )
        runs = [
            ("sympla", "vitoria", now - timedelta(hours=1), now - timedelta(minutes=50), 0),
            ("sympla", "sao-paulo", now - timedelta(hours=9), now - timedelta(hours=8), 0),
            ("zig", "vitoria", now - timedelta(minutes=5), now - timedelta(minutes=4), 3),
        ]
        for source, city, started, finished, errors in runs:
            conn.execute(
                text(
                    "insert into crawl_runs (source, city, kind, started_at, finished_at, "
                    "errors, error_detail) values (:s, :c, 'crawl', :a, :b, :e, :d)"
                ),
                {
                    "s": source,
                    "c": city,
                    "a": started,
                    "b": finished,
                    "e": errors,
                    "d": "first line\nsecond" if errors else None,
                },  # fmt: skip
            )
        conn.execute(
            text(
                "insert into scheduler_heartbeat (id, started_at, beat_at, git_sha, jobs) "
                "values (1, :a, :b, 'abc1234', 11)"
            ),
            {"a": now - timedelta(hours=2), "b": now - timedelta(seconds=40)},
        )
    monkeypatch.setattr(status, "detect_mode", lambda: "inside")

    report = status.build_report(None, dsn=dsn)
    assert report.mode == "inside" and report.containers == {}
    assert report.scheduler_up and report.scheduler.up and report.scheduler.container == ""
    assert 40 <= report.scheduler.age_seconds < 70 and report.scheduler.jobs == 11
    assert report.scheduler.git_sha == "abc1234"
    assert report.totals["crawl_runs"] == 3 and report.totals["events_active"] == 0

    cities = {(c.source, c.city): c for c in report.cities}
    assert set(cities) == {
        ("sympla", "vitoria"),
        ("sympla", "sao-paulo"),
        ("zig", "vitoria"),
        ("uticket", ""),
    }
    assert cities[("sympla", "vitoria")].verdict == "fresh"
    assert cities[("sympla", "sao-paulo")].verdict == "stale"
    assert cities[("zig", "vitoria")].verdict == "errors"
    assert cities[("zig", "vitoria")].error_line == "first line"
    assert cities[("uticket", "")].verdict == "never"
    assert cities[("sympla", "vitoria")].sla_seconds == 6 * 3600

    sources = {s.source: s for s in report.sources}
    assert set(sources) == {"sympla", "zig", "uticket"}  # blueticket is disabled
    assert sources["sympla"].verdict == "stale" and sources["sympla"].by_verdict == {
        "fresh": 1,
        "stale": 1,
    }
    assert sources["zig"].verdict == "errors" and sources["uticket"].verdict == "never"
    assert report.problems == [
        "sympla: stale in 1/2 cities (sao-paulo)",
        "uticket: never in 1/1 cities (-)",
        "zig: errors in 1/1 cities (vitoria)",
    ]

    # --hours 12 makes sao-paulo fresh; --hours 0.5 makes vitoria stale as well.
    wide = status.build_report(12, dsn=dsn)
    assert {s.source: s.verdict for s in wide.sources}["sympla"] == "fresh"
    narrow = status.build_report(0.5, dsn=dsn)
    assert "sympla: stale in 2/2 cities (sao-paulo, vitoria)" in narrow.problems

    # A stale heartbeat is the first problem; a missing row is "never beaten".
    with scratch_engine.begin() as conn:
        conn.execute(text("update scheduler_heartbeat set beat_at = now() - interval '6 minutes'"))
    stale = status.build_report(None, dsn=dsn)
    assert not stale.scheduler_up and stale.problems[0].startswith(
        "scheduler is down: last heartbeat 6m ago"
    )
    with scratch_engine.begin() as conn:
        conn.execute(text("delete from scheduler_heartbeat where id = 1"))
    gone = status.build_report(None, dsn=dsn)
    assert gone.scheduler.age_seconds == -1 and gone.problems[0].startswith(
        "scheduler has never beaten"
    )
