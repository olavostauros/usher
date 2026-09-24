@AGENTS.md

# Claude Code notes for this repository

AGENTS.md above is the project's instruction file for every coding agent; this
file only imports it and adds what Claude Code needs on top. The import stays
so that a Claude Code older than 2.1.277 (which does not read AGENTS.md by
itself) gets the same rules.

## Where you are

Check `hostname` first; the rules below depend on it.

**Inside the `dev` container (`hostname` is `evcrawl-dev`)** — the normal case
for every member and their agent (AGENTS.md §4):

- Logged in as your own Linux user, working in your clone at `~/usher`. No
  `docker`, no `sudo`, no `apt`; the image is rebuilt from a PR when it needs a
  tool.
- `DATABASE_URL`, `REDIS_URL`, `NOMINATIM_URL`, `EVCRAWL_*` and `PG*` are preset
  by the container entrypoint. `psql` and `redis-cli -u "$REDIS_URL"` work as-is.
- The `db` service is shared with the team. Read freely; write to `public.*` only
  through the pipeline, with `--dry-run` or `--limit`; experiments go in a schema
  named after your login. DB-backed tests need `EVCRAWL_TEST_DATABASE_URL`
  pointing at `createdb -T template_postgis <login>_scratch`; without it they
  are skipped. You connect as the non-superuser `team` role: no DDL in `public`.

**On the host, in the owner's checkout (any other `hostname`)** — only the host
owner works here, and only for host-owner tasks (AGENTS.md §4 "Host owner's
sessions"): release smoke, security audits, compose operations, host docs.

- Nothing is preset: no `DATABASE_URL`, no `PG*`. The compose `db` publishes no
  host port, so `psql` and the DB-backed tests do not reach it from here; run
  them inside the container, or with the one-off `docker run` recipe in
  `ops/host/README.md` (the owner runs `docker`, not the agent).
- The same guard hook and rules apply: no bare `docker` from the agent (the
  owner runs compose and builds by hand; `ops/audit/audit.py` and
  `ops/host/status.py` wrap their own read-only probes), no signing, no
  merging. "Is it up / is it crawling?" is answered by `python3
  ops/host/status.py`, not by asking the owner to paste `docker compose ps`.
  Releasing (build, migrate, smoke, start the scheduler) is
  `ops/host/release.sh`, which the owner runs.
- Feature work still happens in the container. If you are here to build a
  spec task, stop and say so.

## Gates

Run these from the repo root before calling anything done:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```

## Current work

Phase 3 (national coverage, monitoring, freshness SLAs) is in progress; the
three specs run in order, `docs/specs/phase-3-national.md` →
`phase-3-monitoring.md` → `phase-3-freshness.md`, each additive schema
change in its own migration (`0003` to `0006` so far). The national
spec's code is all landed (2026-09-23); what remains there is the owner's
first announced cycle and the measured tables in its §2. The monitoring
spec's code is all landed: task 1 as migration `0005` (PR #39: views,
`scheduler_heartbeat`, `grafana_ro`), task 2 as the `heartbeat`
scheduler job plus `ops/host/status.py` on the views (PR #41), task 3
as the `grafana` compose service provisioned from `ops/grafana/`
(PR #42, "rebuild required"), task 4 as the four dashboard JSONs under
`ops/grafana/dashboards/`, migration `0006` (three more views the
Catalog and Geocoder panels needed) and `tests/test_grafana_queries.py`
(PR #43), and task 5, the close-out (`release.sh` prints the Grafana
URL, the audit scope note in `docs/security/README.md`, AGENTS.md §5
host commands; branch `feat/monitoring-closeout`). Grafana is not
started and the dashboards are not rendered until the owner's next
`ops/host/release.sh` (which applies `0005` and `0006` to the shared
`db`) and `docker compose up -d grafana`; until then the views,
`status.py` and the dashboards only work on a scratch database, and the
monitoring spec's §5 acceptance is the owner's host checklist (on
2026-09-24 `status.py` showed `grafana` healthy and the scheduler on
`e9d7c0f`).

**Phase 4, the read-only events API for the Usher PWA, is next** (scoped
2026-09-24): `docs/specs/phase-4-api.md`, ADR 0014, and the PWA's contract
in `docs/api/README.md`. **Next to build is its task 1**: the `api_*` views
and the `api_ro` role in the next free migration (`0007`), following the
`grafana_ro` pattern of `0005` and `db/roles.py`. Tasks 1-4 are feature
work for the `dev` container. Task 5 (public exposure) waits for the owner's
decision in spec §3 "Open". The freshness spec
(`docs/specs/phase-3-freshness.md`) is paused until API task 4 lands, and
its `sla` migration takes whatever number is free then. Phases 0-2 and the rules they left behind are summarised
in AGENTS.md §12; the whole stack — workspace, `db`, `redis`, `nominatim`
and the `evcrawl` release container — is the Compose project on the owner's
computer, and there is no other deployment target. Adding or changing an
adapter follows the `add-source-adapter` skill. Security findings are
queued in `docs/security/reports/`, newest last; fixing one is its own PR
(`docs/security/README.md`).

## Guardrails

`.claude/hooks/guard.py` enforces the rules in AGENTS.md §4 "Agents in the
workspace" on every shell command (crawls without `--dry-run`/`--limit`, the
scheduler, destructive SQL, force pushes, paths outside your home). A blocked
command means the approach needs to change, not the hook.
