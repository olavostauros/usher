#!/usr/bin/env python3
"""Read-only status of the evcrawl stack: is it up, and is it crawling?

Two modes, chosen automatically:
  host   - on the machine that runs docker: container state via
           `docker compose ps`, database figures via `docker compose exec db psql`.
  inside - in the dev container as a member: `psql` with the preset PG* variables;
           container state is not visible from there and is reported as such.

Every probe is a `docker compose ps` or a SELECT. Nothing is written. Like
ops/audit/audit.py, this is the one way an agent on the host may look at the
stack (CLAUDE.md, "On the host").

    ops/host/status.py            # table
    ops/host/status.py --json     # machine-readable
    ops/host/status.py --hours 6  # freshness window (AGENTS.md §9: 6 h per source)

Exit code: 0 healthy, 1 something needs attention (scheduler down, a source
stale or erroring), 2 could not probe. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

SERVICES = ["db", "redis", "nominatim", "dev", "evcrawl"]

LAST_RUNS_SQL = """
select distinct on (s.name)
       s.name,
       coalesce(to_char(r.started_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI'), ''),
       coalesce(extract(epoch from now() - coalesce(r.finished_at, r.started_at))::int, -1),
       coalesce(r.finished_at is null, false),
       coalesce(r.discovered, 0), coalesce(r.fetched, 0), coalesce(r.upserted, 0),
       coalesce(r.errors, 0)
  from sources s
  left join crawl_runs r on r.source = s.name
 where s.enabled
 order by s.name, r.started_at desc nulls last
"""

TOTALS_SQL = """
select (select count(*) from events),
       (select count(*) from events where dedupe_review),
       (select count(*) from events where starts_at >= now()),
       (select count(*) from source_listings),
       (select count(*) from venues),
       (select count(*) from crawl_runs)
"""


@dataclass
class SourceStatus:
    source: str
    last_started: str
    age_seconds: int
    running: bool
    discovered: int
    fetched: int
    upserted: int
    errors: int
    verdict: str  # fresh | running | stale | never | errors


@dataclass
class Report:
    mode: str
    containers: dict[str, str]
    scheduler_up: bool | None
    totals: dict[str, int]
    sources: list[SourceStatus]
    problems: list[str]


def run(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, (p.stdout + p.stderr).strip()


def detect_mode() -> str:
    if shutil.which("docker") and run(["docker", "compose", "ps", "--format", "json"])[0] == 0:
        return "host"
    if shutil.which("psql") and os.environ.get("PGHOST"):
        return "inside"
    return "none"


def container_states() -> dict[str, str]:
    code, out = run(["docker", "compose", "ps", "--all", "--format", "json"])
    if code != 0:
        return {}
    rows: list[dict[str, str]] = []
    text = out.strip()
    if text.startswith("["):  # older compose prints one array
        rows = json.loads(text)
    else:  # newer compose prints one object per line
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    states = {svc: "absent" for svc in SERVICES}
    for row in rows:
        svc = row.get("Service", "")
        if svc in states:
            states[svc] = row.get("State", "?") + (
                f" ({row['Health']})" if row.get("Health") else ""
            )
    return states


def psql(mode: str, sql: str) -> list[list[str]]:
    flags = ["-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    if mode == "host":
        argv = ["docker", "compose", "exec", "-T", "db", "psql", "-U", "evcrawl", "-d", "evcrawl"]
        argv += flags
    else:
        argv = ["psql", *flags]
    code, out = run(argv)
    if code != 0:
        raise RuntimeError(f"psql failed ({code}): {out[:300]}")
    return [line.split("\t") for line in out.splitlines() if line]


def source_statuses(rows: list[list[str]], hours: float) -> list[SourceStatus]:
    out: list[SourceStatus] = []
    for name, started, age, running, disc, fetched, upserted, errors in rows:
        st = SourceStatus(
            name,
            started,
            int(age),
            running == "t",
            int(disc),
            int(fetched),
            int(upserted),
            int(errors),
            "fresh",
        )
        if st.age_seconds < 0:
            st.verdict = "never"
        elif st.running:
            st.verdict = "running"
        elif st.errors:
            st.verdict = "errors"
        elif st.age_seconds > hours * 3600:
            st.verdict = "stale"
        out.append(st)
    return out


def build_report(hours: float) -> Report:
    mode = detect_mode()
    if mode == "none":
        raise RuntimeError("neither docker compose nor a preset psql is available here")
    containers = container_states() if mode == "host" else {}
    scheduler_up = containers.get("evcrawl", "").startswith("running") if containers else None

    t = psql(mode, TOTALS_SQL)[0]
    keys = ["events", "events_under_review", "events_upcoming", "listings", "venues", "crawl_runs"]
    totals = dict(zip(keys, map(int, t), strict=True))
    sources = source_statuses(psql(mode, LAST_RUNS_SQL), hours)

    problems: list[str] = []
    if scheduler_up is False:
        problems.append("scheduler container evcrawl is not running (ops/host/release.sh)")
    for svc, state in containers.items():
        if svc != "evcrawl" and not state.startswith("running"):
            problems.append(f"container {svc} is {state}")
    for s in sources:
        if s.verdict in ("stale", "never", "errors"):
            problems.append(f"{s.source}: {s.verdict} (last run {s.last_started or 'none'})")
    if not sources:
        problems.append("no enabled sources in the database yet (no crawl has run)")
    return Report(mode, containers, scheduler_up, totals, sources, problems)


def fmt_age(seconds: int) -> str:
    if seconds < 0:
        return "-"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def print_table(r: Report) -> None:
    print(f"mode: {r.mode}")
    if r.containers:
        print("containers: " + ", ".join(f"{k}={v}" for k, v in r.containers.items()))
    else:
        print("containers: not visible from inside the workspace")
    print("totals: " + ", ".join(f"{k.replace('_', ' ')}={v}" for k, v in r.totals.items()))
    print()
    print(
        f"{'source':<12}{'last run (UTC)':<18}{'age':>7}{'disc':>6}{'fetch':>6}"
        f"{'upsert':>7}{'err':>5}  verdict"
    )
    for s in r.sources:
        print(
            f"{s.source:<12}{s.last_started:<18}{fmt_age(s.age_seconds):>7}"
            f"{s.discovered:>6}{s.fetched:>6}{s.upserted:>7}{s.errors:>5}  {s.verdict}"
        )
    print()
    if r.problems:
        for p in r.problems:
            print(f"!! {p}")
    else:
        print("ok: every enabled source crawled within the window, no errors")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument(
        "--hours", type=float, default=6.0, help="freshness window per source (default 6)"
    )
    args = ap.parse_args()
    try:
        report = build_report(args.hours)
    except RuntimeError as e:
        print(f"status: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print_table(report)
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
