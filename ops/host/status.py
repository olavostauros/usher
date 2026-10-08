#!/usr/bin/env python3
"""Read-only status of the evcrawl stack: is it up, and is it crawling?

Two modes, chosen automatically:
  host   - on the machine that runs docker: container state via
           `docker compose ps` (the events API shows as `api`, healthy once its
           /healthz answers), database figures via `docker compose exec db psql`.
  inside - in the dev container as a member: `psql` with the preset PG* variables;
           container state is not visible from there, but the scheduler is:
           it beats into `scheduler_heartbeat` every minute.

Every figure is a SELECT on the monitoring views of migration 0005
(`v_source_city_freshness`, `v_scheduler`, `v_catalog_by_city`,
`v_geocode_health`, `v_crawl_runs_hourly`; `VIEWS` in `evcrawl.db.models`), so
this script, Grafana and `evcrawl health` cannot disagree. Nothing is written.
Like ops/audit/audit.py, this is the one way an agent on the host may look at
the stack (AGENTS.md §4, "Where you are").

    ops/host/status.py                 # per-source roll-up over its cities
    ops/host/status.py --city          # every source x city row
    ops/host/status.py --city sao-paulo
    ops/host/status.py --json          # machine-readable: sources, cities, scheduler
    ops/host/status.py --hours 3       # override the views' sla_seconds

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
from collections import Counter
from dataclasses import asdict, dataclass, field

SERVICES = ["db", "redis", "dev", "grafana", "evcrawl", "api", "cloudflared"]
VERDICTS = ("fresh", "running", "stale", "never", "errors")
HEARTBEAT_DOWN_SECONDS = 300  # spec 3 `heartbeat` SLA: beat_at at most 5 min old

# Booleans come out as t/f, nulls as '', timestamps as 'YYYY-MM-DD HH24:MI' UTC.
FRESHNESS_SQL = """
select source,
       coalesce(city, ''),
       coalesce(to_char(started_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI'), ''),
       coalesce(age_seconds, -1),
       running,
       coalesce(to_char(running_since at time zone 'UTC', 'YYYY-MM-DD HH24:MI'), ''),
       coalesce(discovered, 0), coalesce(fetched, 0), coalesce(skipped, 0),
       coalesce(upserted, 0), coalesce(removed, 0), coalesce(errors, 0),
       coalesce(error_line, ''),
       sla_seconds,
       verdict
  from v_source_city_freshness
 order by source, city nulls first
"""

SCHEDULER_SQL = """
select to_char(started_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI'),
       to_char(beat_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
       git_sha,
       jobs,
       age_seconds
  from v_scheduler
"""

TOTALS_SQL = """
select coalesce(sum(events_active), 0),
       coalesce(sum(events_upcoming_7d), 0),
       coalesce(sum(events_under_review), 0),
       coalesce(sum(events_removed), 0),
       coalesce(sum(listings), 0),
       (select venues_total from v_geocode_health),
       (select venues_without_location from v_geocode_health),
       (select coalesce(sum(runs), 0) from v_crawl_runs_hourly)
  from v_catalog_by_city
"""
TOTAL_KEYS = [
    "events_active",
    "events_upcoming_7d",
    "events_under_review",
    "events_removed",
    "listings",
    "venues",
    "venues_without_location",
    "crawl_runs",
]


@dataclass
class CityStatus:
    source: str
    city: str  # '' for whole-source runs (before 0003 or `evcrawl crawl <source>`)
    last_started: str
    age_seconds: int
    running: bool
    running_since: str  # '' unless a newer run of this city is still open
    discovered: int
    fetched: int
    skipped: int
    upserted: int
    removed: int
    errors: int
    error_line: str
    sla_seconds: int
    verdict: str  # fresh | running | stale | never | errors (from the view)


@dataclass
class SourceStatus:
    """One source over its cities: the roll-up the default table prints."""

    source: str
    last_started: str  # of the most recent finished city run
    age_seconds: int  # of that run; -1 when no city has finished a run
    cities: int
    discovered: int
    fetched: int
    upserted: int
    errors: int
    by_verdict: dict[str, int]
    verdict: str  # worst verdict over the cities, see rollup()


@dataclass
class SchedulerStatus:
    up: bool
    container: str  # compose state in host mode, '' inside
    started_at: str
    beat_at: str
    age_seconds: int  # -1 when the row does not exist (never beat)
    git_sha: str
    jobs: int


@dataclass
class Report:
    mode: str
    containers: dict[str, str]
    scheduler_up: bool
    scheduler: SchedulerStatus
    totals: dict[str, int]
    sources: list[SourceStatus]
    cities: list[CityStatus]
    problems: list[str] = field(default_factory=list)


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


def psql(mode: str, sql: str, dsn: str | None = None) -> list[list[str]]:
    """Run *sql* and return its rows as psql prints them (tab-separated, t/f, '' for null).

    *dsn* is a libpq URI for the tests, which point this at their scratch
    database; nothing else passes it.
    """
    flags = ["-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    if mode == "host":
        argv = ["docker", "compose", "exec", "-T", "db", "psql", "-U", "evcrawl", "-d", "evcrawl"]
        argv += flags
    else:
        argv = ["psql", *flags, *([dsn] if dsn else [])]
    code, out = run(argv)
    if code != 0:
        raise RuntimeError(f"psql failed ({code}): {out[:300]}")
    return [line.split("\t") for line in out.splitlines() if line]


def city_statuses(rows: list[list[str]], hours: float | None) -> list[CityStatus]:
    """Parse FRESHNESS_SQL rows; *hours* overrides the view's sla_seconds when given."""
    out: list[CityStatus] = []
    for row in rows:
        (source, city, started, age, running, since, disc, fetched, skipped, upserted, removed,
         errors, error_line, sla, verdict) = row  # fmt: skip
        st = CityStatus(
            source,
            city,
            started,
            int(age),
            running == "t",
            since,
            int(disc),
            int(fetched),
            int(skipped),
            int(upserted),
            int(removed),
            int(errors),
            error_line,
            int(sla),
            verdict,
        )
        if hours is not None:
            st.sla_seconds = int(hours * 3600)
            if st.verdict in ("fresh", "stale"):  # the other three do not depend on the SLA
                st.verdict = "stale" if st.age_seconds > st.sla_seconds else "fresh"
        out.append(st)
    return out


def rollup(cities: list[CityStatus]) -> list[SourceStatus]:
    """One row per source: the newest finished run, the counts summed, the worst verdict.

    Precedence: any city ``errors`` > any ``stale`` > every city ``never`` >
    any ``running`` > ``fresh``; a source whose cities are all fine but one is
    the problem, so the roll-up says so and ``--city`` shows which.
    """
    by_source: dict[str, list[CityStatus]] = {}
    for c in cities:
        by_source.setdefault(c.source, []).append(c)
    out: list[SourceStatus] = []
    for source, rows in by_source.items():
        finished = [r for r in rows if r.age_seconds >= 0]
        latest = min(finished, key=lambda r: r.age_seconds) if finished else None
        counts = Counter(r.verdict for r in rows)
        if counts["errors"]:
            verdict = "errors"
        elif counts["stale"]:
            verdict = "stale"
        elif counts["never"] == len(rows):
            verdict = "never"
        elif counts["running"]:
            verdict = "running"
        else:
            verdict = "fresh"
        out.append(
            SourceStatus(
                source,
                latest.last_started if latest else "",
                latest.age_seconds if latest else -1,
                len(rows),
                sum(r.discovered for r in rows),
                sum(r.fetched for r in rows),
                sum(r.upserted for r in rows),
                sum(r.errors for r in rows),
                {v: counts[v] for v in VERDICTS if counts[v]},
                verdict,
            )
        )
    return out


def scheduler_status(rows: list[list[str]], container: str) -> SchedulerStatus:
    if not rows:
        return SchedulerStatus(False, container, "", "", -1, "", 0)
    started, beat, sha, jobs, age = rows[0]
    return SchedulerStatus(
        int(age) <= HEARTBEAT_DOWN_SECONDS, container, started, beat, int(age), sha, int(jobs)
    )


def build_report(hours: float | None, dsn: str | None = None) -> Report:
    mode = detect_mode()
    if mode == "none":
        raise RuntimeError("neither docker compose nor a preset psql is available here")
    containers = container_states() if mode == "host" else {}

    totals = dict(zip(TOTAL_KEYS, map(int, psql(mode, TOTALS_SQL, dsn)[0]), strict=True))
    cities = city_statuses(psql(mode, FRESHNESS_SQL, dsn), hours)
    sources = rollup(cities)
    sched = scheduler_status(psql(mode, SCHEDULER_SQL, dsn), containers.get("evcrawl", ""))

    problems: list[str] = []
    if not sched.up:
        if sched.age_seconds < 0:
            problems.append("scheduler has never beaten (ops/host/release.sh starts it)")
        elif sched.container.startswith("running"):
            problems.append(
                f"scheduler heartbeat is {fmt_age(sched.age_seconds)} old while the evcrawl "
                "container is running: wedged, restart it (ops/host/release.sh)"
            )
        else:
            problems.append(
                f"scheduler is down: last heartbeat {fmt_age(sched.age_seconds)} ago "
                "(ops/host/release.sh)"
            )
    for svc, state in containers.items():
        if svc != "evcrawl" and not state.startswith("running"):
            problems.append(f"container {svc} is {state}")
    for s in sources:
        if s.verdict in ("stale", "never", "errors"):
            bad = [c.city or "-" for c in cities if c.source == s.source and c.verdict == s.verdict]
            where = ", ".join(bad[:5]) + (f" +{len(bad) - 5}" if len(bad) > 5 else "")
            problems.append(f"{s.source}: {s.verdict} in {len(bad)}/{s.cities} cities ({where})")
    if not sources:
        problems.append("no enabled sources in the database yet (no crawl has run)")
    return Report(mode, containers, sched.up, sched, totals, sources, cities, problems)


def fmt_age(seconds: int) -> str:
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def fmt_scheduler(s: SchedulerStatus) -> str:
    state = "up" if s.up else "down"
    if s.age_seconds < 0:
        detail = "no heartbeat yet"
    else:
        detail = f"heartbeat {fmt_age(s.age_seconds)}, {s.jobs} jobs, build {s.git_sha}"
    if s.container:
        detail = f"container {s.container}, {detail}"
    return f"scheduler: {state} ({detail})"


def print_sources(r: Report) -> None:
    print(
        f"{'source':<12}{'last run (UTC)':<18}{'age':>7}{'cities':>7}{'disc':>7}{'fetch':>7}"
        f"{'upsert':>7}{'err':>5}  verdict"
    )
    for s in r.sources:
        breakdown = ", ".join(f"{n} {v}" for v, n in s.by_verdict.items())
        print(
            f"{s.source:<12}{s.last_started:<18}{fmt_age(s.age_seconds):>7}{s.cities:>7}"
            f"{s.discovered:>7}{s.fetched:>7}{s.upserted:>7}{s.errors:>5}  {s.verdict}"
            f"  ({breakdown})"
        )


def print_cities(r: Report, city: str) -> None:
    rows = r.cities if city == "all" else [c for c in r.cities if c.city == city]
    print(
        f"{'source':<12}{'city':<18}{'last run (UTC)':<18}{'age':>7}{'disc':>6}{'fetch':>6}"
        f"{'skip':>6}{'upsert':>7}{'rm':>4}{'err':>5}  verdict"
    )
    for c in rows:
        last = c.last_started or (f"open {c.running_since[11:]}" if c.running_since else "")
        print(
            f"{c.source:<12}{c.city or '-':<18}{last:<18}{fmt_age(c.age_seconds):>7}"
            f"{c.discovered:>6}{c.fetched:>6}{c.skipped:>6}{c.upserted:>7}{c.removed:>4}"
            f"{c.errors:>5}  {c.verdict}" + (f"  {c.error_line[:60]}" if c.error_line else "")
        )
    if city != "all" and not rows:
        print(f"(no crawl run for city {city!r}; slugs are the registry's, e.g. sao-paulo)")


def print_table(r: Report, city: str | None) -> None:
    print(f"mode: {r.mode}")
    if r.containers:
        print("containers: " + ", ".join(f"{k}={v}" for k, v in r.containers.items()))
    else:
        print("containers: not visible from inside the workspace")
    print(fmt_scheduler(r.scheduler))
    print("totals: " + ", ".join(f"{k.replace('_', ' ')}={v}" for k, v in r.totals.items()))
    print()
    if city is None:
        print_sources(r)
    else:
        print_cities(r, city)
    print()
    if r.problems:
        for p in r.problems:
            print(f"!! {p}")
    else:
        print("ok: scheduler beating, every source fresh in every city, no errors")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument(
        "--hours",
        type=float,
        default=None,
        help="freshness window per source x city, overriding the views' sla_seconds (6 h)",
    )
    ap.add_argument(
        "--city",
        nargs="?",
        const="all",
        default=None,
        metavar="SLUG",
        help="per-city table instead of the per-source roll-up; a slug filters to that city",
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
        print_table(report, args.city)
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
