# Phase 3 build spec (3/3): freshness SLAs and alerting

Status: paused 2026-09-24 behind Phase 4 (landed), then behind the Google
Cloud move (`DEPLOY.md`); the owner says when it resumes. Ready to build
otherwise; the `sla` migration takes the next free number (`0011` on
2026-10-07). Written 2026-09-22. Third of the three Phase 3 specs;
depends on migration `0003` (`phase-3-national.md` task 3, merged 2026-09-23)
and the views and heartbeat of `phase-3-monitoring.md`; the `sla` table is
additive and goes in its own migration (see that spec's §3). Its last task closes Phase 3. Execute
the tasks in order, one branch and PR per task. AGENTS.md rules apply
throughout.

## 1. Goal

`evcrawl health` exits 0 when every SLA holds and 1 with a JSON list of
breaches when one does not; the scheduler runs it every 15 minutes and posts
each breach once, and its recovery once, to the team's Discord channel through
a webhook; the `evcrawl` container reports `healthy` in the compose listing
and `ops/host/status.py`, so "is it crawling?" has a yes/no answer from the
host and from inside the workspace. AGENTS.md §9's freshness targets become a
table the code, the views and the dashboards share.

Out of scope: any alert destination other than the one webhook, paging or
on-call, alert rules inside Grafana (the owner chose alerting in `evcrawl`).

## 2. Facts (2026-09-22; verify before relying on them)

- **Targets today** (AGENTS.md §9): every active source re-crawled at least
  every 6 hours; events starting within 48 hours re-checked every hour. The
  scheduler implements both (`CRAWL_EVERY_HOURS = 6`, `REFRESH_WINDOW =
  48 h`, `run_refresh` hourly). Nothing checks that they are met; status.py
  applies a 6 h window per source by hand.
- **What is measurable after specs 1 and 2**: per source × city last
  successful crawl (`v_source_city_last_run`), per-run counts including
  `skipped`, `removed_at` per listing, the scheduler heartbeat, venues
  without location (`v_geocode_health`).
- **The `evcrawl` service** has no healthcheck; `restart: unless-stopped`
  keeps it up but a wedged scheduler process stays "running". The image's
  entrypoint is the CLI, so a `HEALTHCHECK` can be an `evcrawl` subcommand.
- **Redis** (`redis:7-alpine`, `REDIS_URL` preset for members and the
  release) is unused by the code; the security queue (F-013) notes it is
  unauthenticated. A key holding the last alert state is not sensitive.
- **The team channel** is where crawls and rebuilds are announced
  (AGENTS.md §4, §11, `ops/host/README.md`); the owner chose a Discord
  webhook as the alert destination. A Discord webhook is a `POST` of
  `{"content": "..."}` (≤ 2000 chars) to a URL that is itself the secret; it
  belongs in the host `.env`, never in the image, the repo or a member's
  environment (`team/entrypoint.sh` must not export it).
- **Settings** come from the environment only (`src/evcrawl/config.py`,
  pydantic-settings); `.env.example` documents every variable.

## 3. Design decisions

Owner decision of 2026-09-22: breaches and recoveries go to a Discord
webhook, sent by `evcrawl health` from the scheduler; silent when healthy.

- **The SLA table** is code first: `src/evcrawl/sla.py` declares
  `SLA(name, scope, threshold, description)` rows, mirrored into a small
  `sla` table by its own migration (seeded by the migration and re-synced by
  `evcrawl migrate` from the code so the views can join it):

  | name | scope | threshold | source of the measure |
  |---|---|---|---|
  | `crawl` | source × city | last successful crawl ≤ 6 h old | `v_source_city_last_run` |
  | `refresh` | source | last refresh run ≤ 1 h old, or no event within 48 h | `crawl_runs` kind `refresh` |
  | `removal` | source × city | a listing not seen for two error-free cycles has `removed_at` | `source_listings` vs last runs |
  | `heartbeat` | scheduler | `beat_at` ≤ 5 min old | `v_scheduler` |
  | `geocode` | global | ≥ 95 % of active events' venues have a location | `v_geocode_health` |
  | `errors` | source | errors / discovered over the last 3 crawl runs < 20 % | `v_crawl_runs_hourly` |

  Per-city overrides are rows with a `city` (Greater Vitória keeps 6 h; a
  capital a source does not list is not a breach because there is no job for
  it). Changing a threshold is a change to AGENTS.md §9 and to this table in
  one commit.
- **`evcrawl health`**: reads the views, evaluates every SLA, prints
  `{"ok": bool, "checked_at", "breaches": [{"sla", "source", "city",
  "measure", "threshold", "since", "detail"}], "recovered": [...]}` and
  exits 0/1 (2 when the db is unreachable). `--quiet` prints nothing (for the
  container healthcheck). `--notify` compares against the last known state
  and posts transitions. Never includes event content, organizer or personal
  data; `detail` is the first line of `error_detail` or an age.
- **State in Redis**: key `evcrawl:health:state`, a JSON map `breach key →
  since`; `--notify` posts a breach when it enters the map and a recovery
  when it leaves, so a persistent breach is posted once. Redis down = log a
  warning and post nothing (the container healthcheck still reflects the
  breach). First real use of `REDIS_URL`; no queue, no RQ.
- **The notifier** (`src/evcrawl/notify.py`): one `post(text)` over the
  shared `Fetcher` (`check_robots=False`, its own bucket `discord`, 1 req/s,
  3 retries) to `EVCRAWL_ALERT_WEBHOOK_URL`; unset = notifications disabled
  with one `health.notify_disabled` log at startup. Message: one line per
  transition, e.g. `⚠ crawl sympla/sao-paulo: last success 7h12m ago (SLA 6h);
  last error: fetch https://… HTTP 503` and `✓ crawl sympla/sao-paulo
  recovered`, plus a footer `ops/host/status.py --city sao-paulo`. Batched
  into one post per run, truncated to 2000 chars.
- **Scheduler job `health`** every 15 min (`coalesce=True`,
  `max_instances=1`) calling the same function with notify on; the CLI
  command is what humans and the healthcheck run. Docker `HEALTHCHECK` in
  the runtime stage: `CMD ["evcrawl", "health", "--quiet"]`, interval 5 min,
  timeout 30 s, start period 2 min, retries 2, mirrored in
  `docker-compose.yml`. `status.py` shows the container health next to the
  state; `release.sh` waits up to 3 min for `healthy` after starting the
  scheduler and fails the release otherwise.
- **Runbook** in `ops/host/README.md`, "When an alert fires": run
  `status.py --city <city>`, open the Freshness dashboard, then the three
  usual causes and their moves: a source answering 4xx/5xx or blocking the
  User-Agent → stop, tell the owner, lower the rate or drop the city
  (AGENTS.md §10); the public Nominatim unreachable or refusing us → nothing
  to restart (ADR 0018): the crawl keeps going without coordinates, retries
  those venues next run and the `geocode` SLA reports the backlog;
  a wedged scheduler (heartbeat stale, container running) → `release.sh`.
  Members can diagnose from inside; only the owner restarts anything.

## 4. Tasks

Each task: its own branch (`feat/freshness-<topic>`), gates green in the
`dev` container, PR into `main`.

1. **SLA module + table + verdicts** — `sla.py`, the `sla` table and seed in
   their own migration (`0006`; `0003`-`0005` are merged, see the header),
   `v_source_city_freshness` replaced (`create or replace`) so its
   `sla_seconds` and `verdict` come from the table instead of the constant
   6 h that spec 2 task 1 left there, `evcrawl migrate` re-syncs the rows. Done: unit tests
   on the SLA rows; on scratch, changing a threshold in code and migrating
   changes the view's verdict.
2. **`evcrawl health`** — the command, the evaluators (one per SLA, pure
   functions over view rows so they are unit-testable), JSON output, exit
   codes, `--quiet`. Done: `tests/test_health.py` seeds the scratch db with
   a stale city, a stale heartbeat and a healthy source and asserts the
   breaches list and the exit code; `uv run evcrawl health` inside the
   workspace reports on the shared db.
3. **Notifier + Redis state + scheduler job** — `notify.py`, the Redis
   state, `--notify`, the `health` job, `EVCRAWL_ALERT_WEBHOOK_URL` in
   `config.py` and `.env.example` (host-only comment), `team/entrypoint.sh`
   allowlist unchanged (the variable is not exported to members). Tests use
   `httpx.MockTransport` and a fake Redis (`fakeredis` added with `uv add
   --group dev`, rebuild required) and assert: first breach posts once,
   second run posts nothing, recovery posts once, webhook unset posts
   nothing. Done: on the host, with the real webhook in `.env`, the owner
   disables one source (`update sources set enabled = false`, announced) and
   sees one message within 15 min of the SLA lapsing and one recovery after
   re-enabling and the next successful run.
4. **Healthcheck + release wiring + runbook** — `HEALTHCHECK` in the
   `Dockerfile` runtime stage and `docker-compose.yml`, `status.py` shows
   container health, `release.sh` waits for healthy, the README runbook;
   commit says "rebuild required". Done: `docker compose ps` (owner) shows
   `healthy` for `evcrawl`; stopping the db makes it `unhealthy` within two
   intervals; `release.sh` refuses to finish on an unhealthy scheduler.
5. **Phase close** — AGENTS.md §9 lists the SLA table and names `evcrawl
   health` and the webhook; §12 gains the "Phase 3 (done)" paragraph with
   the ADRs and decisions (registry, cycles, conditional fetch, removal per
   listing, Nominatim, Grafana, health); CLAUDE.md "Current work" moves on;
   each of the three specs gets its "Outcome" paragraph with the date, like
   `phase-2-sources.md`. Done: a member with only the docs can answer "is it
   crawling, is it fresh, who gets told when it is not" in one paragraph.

## 5. Acceptance for the phase (all three specs)

- All four gates green in the `dev` container and in CI.
- Spec 1 §5 and spec 2 §5 hold.
- `evcrawl health` agrees with the Freshness dashboard and status.py on the
  same instant (spot check in the PR of task 2).
- The disable-a-source drill of task 3 produced exactly two Discord messages.
- The `evcrawl` container is `healthy`; a wedged scheduler (heartbeat stale)
  turns it `unhealthy` and posts a `heartbeat` breach.
- No event content, organizer or personal data in any alert or health
  output; the webhook URL appears only in the host `.env`.
- AGENTS.md §9 and §12, CLAUDE.md and the three specs' outcome paragraphs
  updated; ADR 0012 (removal per listing) exists; any decision taken during
  the tasks has its ADR.

## 6. Ask the owner before

Any alert destination other than the one webhook; alert content that names
people or events; changing an SLA threshold (it is an AGENTS.md §9 change);
adding `fakeredis` or any other dependency beyond task 3's; paging, e-mail
or Grafana alerting; running the disable-a-source drill (it changes
`sources.enabled` on the shared db).
