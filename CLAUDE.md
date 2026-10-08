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

**Current task: production moves to Google Cloud** (`DEPLOY.md`, ADR 0019;
the `atlas` agent in `.claude/agents/atlas.md` runs it from the host, the
owner runs everything on the VM). One Spot VM `usher-prod` in
`southamerica-east1` inside the Free Trial credit (R$ 1.779,29, ends
2027-01-06; target ≤ R$ 15/day). State on 2026-10-07: steps 0-4 stand
(project `usher-prod`, budgets `usher-monthly` and `usher-credit` on topic
`usher-budget`, the `usher-budget-cap` Cloud Run function, network
`usher-vpc` with IAP-only SSH); the step 5 VM was created, cap-tested and
deleted the same day because its 80 GB disk was sized for the geocoder
that ADR 0018 removed. **Next: redo step 5** (a 10 GB disk: `pgdata` is
618.8 MB; same name and zone; resume the paused `usher-restart` job once
the VM is up), **then step 6** (`ops/gcp/vm-setup.md`; the old deploy key
and the old tailnet node are both gone), then the data copy, the cut-over
and three days of watching.
Only one scheduler crawls at a time: the host's stops before the VM's
starts (step 9). `DEPLOY.md` §6 is the checklist and §9 the burn log; keep
both current in the same PR as each step.

**End goal behind it:** the Usher PWA answers "events near me" through the
read-only events API (Phase 4, `docs/specs/phase-4-api.md`, ADR 0014, the
PWA's contract in `docs/api/README.md`). Tasks 1-5 landed (PRs #58-#61,
#65): the `api_*` views and `api_ro` (migration `0008`), `evcrawl api`,
`/v1/events` with the keyset cursor and the in-process rate limiter
(migration `0009`), the `api` compose service, and the public URL
`https://api.eventolivre.com` through the Cloudflare Tunnel `usher-api`
(ADR 0015, `cloudflared` compose service, CORS for `https://eventolivre.com`
only). ADR 0016 (migration `0010`, PR #67) makes `events_upcoming` count
what the list returns; the list's window rule and series prefix in
`src/evcrawl/api/events.py` are inlined by the count views, so changing them
is a new migration. The tunnel credentials
(`ops/host/cloudflared/credentials.json`, gitignored, mode 600) are the
owner's: never read, copy or print them. The `cerberus` re-audit of the
public surface is done (2026-10-07 report: every check in
`docs/security/README.md` passes; HEAD requests get 405, check with GET).
Still open from Phase 4: task 6, the phase close.
Freshness spec 3 (`docs/specs/phase-3-freshness.md`; its `sla` migration
takes `0011`) waits for the owner to order it after the move.

**Live state (2026-10-07, host):** the scheduler, the API and the tunnel
run build `682ac4f` (PR #65); `main` is ahead by PRs #67-#74, so migration
`0010`, the Nominatim removal and the Zig test fix are **not released**.
Whether the next `release.sh` runs on the host or first on the VM (step 7
migrates there) is the owner's call. Migrations `0005`-`0009` are on the
shared `db`; `grafana`, `usher-api-1` and `usher-cloudflared-1` are
healthy; `https://api.eventolivre.com/healthz` says `db: ok`. The release
smoke fails on TicketBolt alone (Queue-it waiting room, #55), so
`release.sh` refuses to start the services and the owner runs
`up -d evcrawl api` by hand after every release; pausing the source is the
owner's call (AGENTS.md §10). `release.sh` also refuses a dirty tree.
`python3 ops/host/status.py` is the live view. Owner-only leftovers: the
measured tables in the national spec §2, the monitoring spec §5 host
checklist, retiring the old `nominatim` service and volume on the host
(`ops/host/README.md` "Geocoder", ADR 0018).

**Security queue:** `docs/security/reports/2026-10-07-usher-stack.md`
(the re-audit after the move to the VM, PR #82; the VM's own scanner run
is folded in; "Analyst notes → Take these first" is the order). Open next:
F-015+F-019+F-020 (one bump to `postgis/postgis:16-3.5`, VM first after
its snapshot, then `postgis_extensions_upgrade()`), then F-032+F-016 as
one rebuild/bump day (OpenSSL in the host's runtime image, `cloudflared`, `redis`, the dev
image), then F-033 (one secret set for dev and prod), then F-011+F-010
(sshd); F-027/F-028, F-034, F-035, F-036 and F-038 are owner decisions.
F-030 (the host unit) is fixed in PR #83; the owner reinstalls the unit.
F-031 (Grafana `13.0.10`, digest-pinned) is fixed in PR #85; the owner
pulls and restarts `grafana` on the host and on the VM.
Each fix is its own PR and flips the finding's `Status:` line
(`docs/security/README.md`). Still owner-only from the report: the
superuser role table on the VM, the Cloudflare dashboard (bot rule, HSTS,
connector count) and the tailnet console (the host's `tailscaled` is
offline and still shows the first VM's node).

Phases 0-3 and the rules they left behind are in AGENTS.md §12. Adding or
changing an adapter follows the `add-source-adapter` skill.

## Guardrails

`.claude/hooks/guard.py` enforces the rules in AGENTS.md §4 "Agents in the
workspace" on every shell command (crawls without `--dry-run`/`--limit`, the
scheduler, destructive SQL, force pushes, paths outside your home). A blocked
command means the approach needs to change, not the hook.
