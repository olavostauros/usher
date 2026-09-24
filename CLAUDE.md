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

**End goal:** the Usher PWA answers "events near me" from this database
through the read-only events API (Phase 4: `docs/specs/phase-4-api.md`,
ADR 0014, the PWA's contract in `docs/api/README.md`).

**Next to build: Phase 4 task 1**, branch `feat/api-views`. The five
`api_*` views and the `api_ro` role in migration `0008`, following the
`grafana_ro` pattern of `0005`/`0007` and `db/roles.py` (generalise it to
both roles, `EVCRAWL_API_DB_PASSWORD` in `.env.example`), and `API_VIEWS`
in `db/models.py`. "Done when" is in spec §4: column test, `api_ro` denied
on `events`/`raw_snapshots`/`v_scheduler` but reading every `api_*` view,
and `EXPLAIN` of the radius query using `ix_venues_location`.

**Then, in order:** API task 2 (FastAPI skeleton, `openapi.json`, "rebuild
required") → 3 (`/v1/events`, `/v1/events/{id}`) → 4 (`api` compose
service on the tailnet; the PWA team starts here) → freshness spec 3
(`docs/specs/phase-3-freshness.md`, paused until task 4; its `sla`
migration takes the next free number) → API task 5 (public exposure,
blocked on the owner's decision in spec §3 "Open") → task 6, the phase
close. Spec §6 lists what to ask the owner first.

**Live state (2026-09-24):** Phase 3 national and monitoring code is all
landed and released: migrations `0005`-`0007` are on the shared `db`,
`grafana` is up, the scheduler runs build `d46aee1` (PRs #55 and #56 are
merged but not yet released). `python3 ops/host/status.py` is the live
view; it showed Sympla erroring in 17/33 cities and TicketBolt in 4.
Owner-only leftovers: the measured tables in the national spec §2 and the
monitoring spec §5 host checklist.

**Security queue:** `docs/security/reports/2026-09-24-usher-stack.md`
("Analyst notes → Take these first" is the order). Open next:
F-015+F-019+F-020 (one bump to `postgis/postgis:16-3.5`), then
F-011+F-010 (sshd); F-027/F-028 are owner decisions. Each fix is its own
PR and flips the finding's `Status:` line (`docs/security/README.md`).

Phases 0-3 and the rules they left behind are in AGENTS.md §12. Adding or
changing an adapter follows the `add-source-adapter` skill.

## Guardrails

`.claude/hooks/guard.py` enforces the rules in AGENTS.md §4 "Agents in the
workspace" on every shell command (crawls without `--dry-run`/`--limit`, the
scheduler, destructive SQL, force pushes, paths outside your home). A blocked
command means the approach needs to change, not the hook.
