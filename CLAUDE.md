@AGENTS.md

# Claude Code notes for this repository

AGENTS.md above is the project's instruction file for every coding agent; this
file only imports it and adds what Claude Code needs on top. It exists because
Claude Code before 2.1.277 does not read AGENTS.md by itself.

## Where you are

- The `dev` container (`hostname evcrawl-dev`), logged in as your own Linux user,
  working in your clone at `~/usher`. No `docker`, no `sudo`, no `apt` here; the
  image is rebuilt from a PR when it needs a tool (AGENTS.md §4).
- `DATABASE_URL`, `REDIS_URL`, `NOMINATIM_URL`, `EVCRAWL_*` and `PG*` are preset
  by the container entrypoint. `psql` and `redis-cli -u "$REDIS_URL"` work as-is.
- The `db` service is shared with the team. Read freely; write to `public.*` only
  through the pipeline, with `--dry-run` or `--limit`; experiments go in a schema
  named after your login.

## Gates

Run these from `~/usher` before calling anything done:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```

## Current work

Phase 1: the Sympla adapter and pipeline for Greater Vitória. The build spec is
`docs/specs/phase-1-sympla.md`; execute its tasks in order, one branch and PR per
task. Adding or changing an adapter follows the `add-source-adapter` skill.

## Guardrails

`.claude/hooks/guard.py` enforces the rules in AGENTS.md §4 "Agents in the
workspace" on every shell command (crawls without `--dry-run`/`--limit`, the
scheduler, destructive SQL, force pushes, paths outside your home). A blocked
command means the approach needs to change, not the hook.
