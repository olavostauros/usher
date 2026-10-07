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

**Done:** task 1 (PR #58, migration `0008`: the `api_*` views, `api_ro`,
released 2026-09-24), task 2 (PR #59: `src/evcrawl/api/`, `evcrawl api`,
`/healthz`, `/v1/categories`, `/v1/cities`, the committed
`docs/api/openapi.json`; regenerate it with `evcrawl api --openapi`),
task 3 (PR #60: `/v1/events` and `/v1/events/{id}`,
`src/evcrawl/api/events.py` builds the SQL and the cursor, recurring
sessions collapsed per venue and title, the in-process rate limiter,
migration `0009` `ix_events_venue_id`; API sessions force custom plans
because the generic plan for `/v1/events` blows the 3 s timeout),
task 4 (PR #61, rebuild required: the `api` compose service on
`${DEV_SSH_BIND}:8000` running the release image as `api_ro`, healthcheck
on `/healthz`, explicit `environment:` and no `env_file`; `release.sh`
starts it after the scheduler, `status.py` and `usher.service` know it;
"Events API" in `ops/host/README.md`, the scope note in
`docs/security/README.md`) and
task 5 (PR #65, rebuild required, ADR 0015: `https://api.eventolivre.com`
through the named Cloudflare Tunnel `usher-api`, run by the `cloudflared`
compose service, which reaches `http://api:8000` inside the compose network
and publishes nothing on the host; ingress in `ops/host/cloudflared/config.yml`
404s `/v1/docs` and every other hostname; CORS is the API's own,
`src/evcrawl/api/cors.py`, exactly four headers for `https://eventolivre.com`
only, 429 included; the rate limiter keys on the last `X-Forwarded-For`
address; runbook in `ops/host/README.md` "Public exposure"). The tunnel
credentials are the owner's (`ops/host/cloudflared/credentials.json`,
gitignored, mode 600): never read, copy or print them. After task 5, ADR
0016 (migration `0010`, needs a release with `evcrawl migrate`):
`events_upcoming` in `/v1/cities` and `/v1/categories` counts the rows the
list returns over its widest window, a recurring series once, and a
running event stays listed until its end, or six hours after its start
without one; the list's window rule and the series prefix live in
`src/evcrawl/api/events.py`, and the count views inline them, so changing
either there is a new migration (`tests/test_api_views.py` holds them equal).

**Next:** spec task 5 still wants a `cerberus` re-audit of the public
surface before the PWA is announced. HEAD requests get 405 (FastAPI GET
routes do not answer HEAD); browsers are unaffected, so check with GET, or
add HEAD support if the owner asks. Then the owner picks the order between
freshness spec 3 (`docs/specs/phase-3-freshness.md`; its `sla` migration
takes the next free number, `0011`) and task 6, the phase close. Spec §6
lists what to ask the owner first.

**Live state (2026-09-25):** the scheduler, the events API and the tunnel
run build `682ac4f` (PR #65 merged). Migrations `0005`-`0009` are on the
shared `db`, `grafana` is up, `usher-api-1` is healthy on
`DEV_SSH_BIND:8000`, and `usher-cloudflared-1` is healthy with one
connector on the São Paulo edge. `https://api.eventolivre.com/healthz` says
`db: ok`; `/v1/categories` from `Origin: https://eventolivre.com` gets 200
with the four CORS headers, from any other origin none. `usher.service`
with `cloudflared` is installed in the owner's systemd user units. The
release smoke fails on TicketBolt alone: the whole site redirects to a
Queue-it waiting room (`e=checkoutslowqueue`, #55), so `release.sh` refuses
to start the services and the owner runs `up -d evcrawl api` by hand after
every release (the tunnel is left alone and reconnects). Pausing the source
is the owner's call (AGENTS.md §10), not code. `release.sh` also refuses a
dirty tree, so the owner stashes local `.claude/settings.json` edits first.
`python3 ops/host/status.py` is the live view. Owner-only leftovers: the
measured tables in the national spec §2, the monitoring spec §5 host
checklist, and `curl http://<host-tailnet-name>:8000/healthz` from a
member's machine.

**Google Cloud (proposed, 2026-10-07):** `DEPLOY.md` moves production to
one VM inside the Free Trial credit (R$ 1.779,29, ends 2027-01-06), ADR
0016. The `atlas` agent (`.claude/agents/atlas.md`) runs its steps from the
host, budgets first, asking before every resource; nothing is created yet.

**Geocoder (2026-10-07):** no self-hosted Nominatim any more (ADR 0018,
supersedes 0017): the public instance at 1 req/s behind `geocode_cache`.
The owner retires the old service and its volume on the host with the
runbook in `ops/host/README.md` "Geocoder".

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
