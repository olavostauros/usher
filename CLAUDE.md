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
  pointing at `createdb <login>_scratch`; without it they are skipped.

**On the host, in the owner's checkout (any other `hostname`)** — only the host
owner works here, and only for host-owner tasks (AGENTS.md §4 "Host owner's
sessions"): release smoke, security audits, compose operations, host docs.

- Nothing is preset: no `DATABASE_URL`, no `PG*`. The compose `db` publishes no
  host port, so `psql` and the DB-backed tests do not reach it from here; run
  them inside the container, or with the one-off `docker run` recipe in
  `ops/host/README.md` (the owner runs `docker`, not the agent).
- The same guard hook and rules apply: no bare `docker` from the agent (the
  owner runs compose and builds by hand; `ops/audit/audit.py` wraps its own
  read-only probes), no signing, no merging.
- Feature work still happens in the container. If you are here to build a
  spec task, stop and say so.

## Gates

Run these from the repo root before calling anything done:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```

## Current work

Phase 2 is done (2026-09-21): five sources — Sympla, uTicket, BlueTicket,
TicketBolt, Zig — crawl Greater Vitória through one pipeline, and cross-source
dedupe merges listings live (ADRs 0003, 0006-0010; AGENTS.md §12 has the
per-source summary and the header decisions). Phase 1's VPS deployment is
still pending; Phase 3 (national coverage, monitoring, freshness SLAs) is
next and has no spec yet. Build specs: `docs/specs/phase-1-sympla.md`,
`docs/specs/phase-2-sources.md`. Adding or changing an adapter follows the
`add-source-adapter` skill. Security findings are queued in
`docs/security/reports/`, newest last; fixing one is its own PR
(`docs/security/README.md`).

## Guardrails

`.claude/hooks/guard.py` enforces the rules in AGENTS.md §4 "Agents in the
workspace" on every shell command (crawls without `--dry-run`/`--limit`, the
scheduler, destructive SQL, force pushes, paths outside your home). A blocked
command means the approach needs to change, not the hook.
