"""The Grafana dashboards query the monitoring views and nothing else (phase 3 monitoring, task 4).

``ops/grafana/dashboards/*.json`` are provisioned as files, so a panel whose
SQL no longer matches a view fails on the host at 3 a.m. and nowhere else.
Here every panel target and every template variable of every dashboard is
loaded, its Grafana macros and variables expanded, and its SQL run through
``EXPLAIN`` and then executed as the ``grafana_ro`` role against the scratch
database at migration head (skipped without ``EVCRAWL_TEST_DATABASE_URL``, like
the other DB tests). The offline tests hold the dashboard contract of the spec
(fixed uid, UTC, one-minute refresh, no UI edits, one datasource) and that no
query names a table.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from evcrawl.db.models import VIEWS

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "ops" / "grafana" / "dashboards"
DASHBOARDS = sorted(DASHBOARD_DIR.glob("*.json"))
EXPECTED = {"freshness", "crawl-runs", "catalog", "geocoder"}
DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "evcrawl"}

# One sample value per template variable, for the queries that filter on them.
SAMPLES = {"source": "sympla", "city": "vitoria", "kind": "crawl"}
FROM_OR_JOIN = re.compile(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def walk_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every panel, including those nested in row panels."""
    out: list[dict[str, Any]] = []
    for panel in panels:
        out.append(panel)
        out.extend(walk_panels(panel.get("panels", [])))
    return out


def queries(dashboard: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(label, format, rawSql)`` for every panel target and template variable."""
    out: list[tuple[str, str, str]] = []
    for panel in walk_panels(dashboard["panels"]):
        for t in panel["targets"]:
            label = f"panel {panel['id']} {panel['title']!r} {t['refId']}"
            out.append((label, t["format"], t["rawSql"]))
    for var in dashboard["templating"]["list"]:
        query = var["query"]
        raw = query["rawSql"] if isinstance(query, dict) else query
        out.append((f"variable ${var['name']}", "table", raw))
    return out


def expand(sql: str) -> str:
    """Replace the Grafana macros and variables the dashboards use with plain SQL."""
    sql = re.sub(
        r"\$__timeFilter\(([^)]+)\)", r"\1 between now() - interval '24 hours' and now()", sql
    )
    sql = re.sub(r"\$\{(\w+):sqlstring\}", lambda m: f"'{SAMPLES[m.group(1)]}'", sql)
    sql = re.sub(r"\$\{(\w+)\}|\$(?!__)(\w+)", lambda m: SAMPLES[m.group(1) or m.group(2)], sql)
    assert "$" not in sql, f"unexpanded macro or variable in: {sql}"
    return sql


def test_the_four_dashboards_are_there() -> None:
    assert {p.stem for p in DASHBOARDS} == EXPECTED


@pytest.mark.parametrize("path", DASHBOARDS, ids=[p.stem for p in DASHBOARDS])
def test_dashboard_contract(path: Path) -> None:
    dashboard = load(path)
    assert dashboard["uid"] == f"evcrawl-{path.stem}"
    assert dashboard["timezone"] == "utc"
    assert dashboard["refresh"] == "1m"
    assert dashboard["editable"] is False
    assert "evcrawl" in dashboard["tags"]
    panels = walk_panels(dashboard["panels"])
    assert panels
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids)), "panel ids must be unique"
    for panel in panels:
        assert panel["title"] and panel["description"], panel["id"]
        assert panel["datasource"] == DATASOURCE, panel["title"]
        assert panel["targets"], panel["title"]
        for t in panel["targets"]:
            assert t["datasource"] == DATASOURCE and t["rawQuery"] is True, panel["title"]
            assert t["format"] in {"table", "time_series"}, panel["title"]
    for var in dashboard["templating"]["list"]:
        assert var["type"] == "query" and var["datasource"] == DATASOURCE, var["name"]
        assert var["name"] in SAMPLES, f"add a sample for ${var['name']} to SAMPLES"


@pytest.mark.parametrize("path", DASHBOARDS, ids=[p.stem for p in DASHBOARDS])
def test_every_query_reads_views_only(path: Path) -> None:
    for label, _, raw in queries(load(path)):
        names = {n.lower() for n in FROM_OR_JOIN.findall(expand(raw))}
        assert names, f"{path.stem}: {label} reads nothing"
        assert names <= set(VIEWS), f"{path.stem}: {label} reads {names - set(VIEWS)}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=[p.stem for p in DASHBOARDS])
def test_every_query_explains_and_runs_as_grafana_ro(path: Path, scratch_engine: Engine) -> None:
    for label, fmt, raw in queries(load(path)):
        sql = expand(raw)
        with scratch_engine.connect() as conn:
            conn.execute(text("set role grafana_ro"))
            try:
                conn.execute(text(f"explain {sql}")).all()
                columns = tuple(conn.execute(text(sql)).keys())
            except Exception as exc:  # noqa: BLE001 - re-raised with the panel named
                raise AssertionError(f"{path.stem}: {label}: {exc}") from exc
            finally:
                conn.rollback()
        if fmt == "time_series":
            assert columns == ("time", "metric", "value"), f"{path.stem}: {label}: {columns}"
        else:
            assert columns, f"{path.stem}: {label} returns no columns"
