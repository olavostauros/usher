# Phase 3 build spec (2/3): monitoring dashboards

Status: ready to build. Written 2026-09-22. Second of the three Phase 3 specs;
depends on migration `0003` from `phase-3-national.md` (merged 2026-09-23 with
the core-table changes; the views, `scheduler_heartbeat` and `grafana_ro` of
task 1 here are additive and go in their own migration, see that spec's §3)
and feeds `phase-3-freshness.md`, whose SLA table the freshness view joins. Execute the tasks in order, one branch and PR per
task. AGENTS.md rules apply throughout.

## 1. Goal

`http://<host-tailnet-ip>:3000` shows, to anyone on the team VPN and without a
login, per source × city freshness against the SLA, crawl runs and errors over
time, catalog counts by city and category, and geocoder health.
`ops/host/status.py`, Grafana and (spec 3) `evcrawl health` read the same SQL
views, so they cannot disagree. "Is the scheduler alive?" becomes a SQL
question answerable from inside the workspace, where containers are invisible.

Out of scope: alerting (spec 3), Prometheus or any metrics endpoint (owner
decision: Grafana reads Postgres directly), log shipping.

## 2. Facts (2026-09-22; verify before relying on them)

- **Monitoring today** is `ops/host/status.py`: `docker compose ps` for
  container states (host mode only) plus two SELECTs (`LAST_RUNS_SQL`: last
  `crawl_runs` row per enabled source; `TOTALS_SQL`: counts), a verdict per
  source (`fresh` | `running` | `stale` | `never` | `errors`) over a
  `--hours` window (default 6), exit 0/1/2. In "inside" mode (members, `psql`
  preset) it cannot tell whether the scheduler container is up. The
  `evcrawl` service has no healthcheck; `db` and `redis` do.
- **Logs** are JSON lines on stdout (`src/evcrawl/logging.py`: structlog,
  `crawl.start`/`crawl.event`/`crawl.done`, `refresh.start`,
  `scheduler.started`); nothing collects them beyond the container's log.
- **`crawl_runs`** has `city` and `kind` since migration `0003` (spec 1 §3,
  merged 2026-09-23); every figure below that says "per city" relies on it.
- **Redis** is in the stack and unused by the code.
- **Host wiring**: `docker-compose.yml` (db, redis, nominatim, dev, evcrawl; `dev` is the
  only service published on the host, at `${DEV_SSH_BIND}:2222`),
  `ops/host/usher.service` (compose up of the four services at boot),
  `ops/host/release.sh` (build, migrate, smoke, start, then status.py).
- **Security queue** (`docs/security/reports/2026-09-20-usher-dev.md`, open):
  F-014 every member is a Postgres superuser, F-001 the superuser password is
  in compose, F-013 Redis is unauthenticated, F-010 unrestricted SSH
  forwarding. A dashboard must not widen any of these: it gets a read-only
  role, and its port follows the sshd rule (tailnet address only).
- **Grafana provisioning** (Grafana OSS docs): files under
  `/etc/grafana/provisioning/{datasources,dashboards,alerting}/`; a
  `postgres` datasource YAML with the password in `secureJsonData`; a
  dashboards provider with `path` to a directory of dashboard JSON,
  `allowUiUpdates`, `updateIntervalSeconds`, `foldersFromFilesStructure`.
  Anonymous access is `GF_AUTH_ANONYMOUS_ENABLED=true` with
  `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`; the admin password is
  `GF_SECURITY_ADMIN_PASSWORD`. Pin the image tag; the task records which.

## 3. Design decisions

Owner decision of 2026-09-22: dashboards are Grafana reading the shared
Postgres through SQL views, provisioned from files in the repo; no Prometheus,
no exporter, no metrics endpoint in `evcrawl`.

- **Views are the contract.** Their migration (`0005`, see task 1) creates,
  and `db/models.py` documents (a `VIEWS` mapping of name → columns):
  - `v_source_city_last_run`: last finished `crawl_runs` row per source × city
    of kind `crawl` (`started_at`, `finished_at`, `age_seconds`, counts,
    `errors`, `error_detail` first line), plus a `running` flag when a newer
    unfinished row exists.
  - `v_source_city_freshness`: the above joined to the `sla` table (spec 3):
    `sla_seconds`, `verdict` (`fresh` | `running` | `stale` | `never` |
    `errors`), so status.py's rule moves into SQL and is shared.
  - `v_crawl_runs_hourly`: per source, per hour bucket: runs, discovered,
    fetched, skipped, upserted, created, merged, removed, errors, mean
    seconds.
  - `v_catalog_by_city`: per venue city + UF: events active, upcoming (7 d),
    under review, removed, listings, venues without location.
  - `v_geocode_health`: venues total, with location, without; cache rows,
    resolved, negative, hit ratio over the last 24 h (`geocode_cache.created_at`).
  - `v_scheduler`: one row from `scheduler_heartbeat` with `age_seconds`.
  Views only read; no materialisation (the tables are small for years).
- **Scheduler heartbeat**: table `scheduler_heartbeat(id smallint primary key
  default 1, started_at, beat_at, git_sha, jobs int)`; a scheduler job
  `heartbeat` upserts the row every minute (`sched.get_jobs()` count,
  `settings.evcrawl_git_sha`). Down = `beat_at` older than 5 min (spec 3 SLA).
- **`grafana_ro` role**: created by the views' migration if absent
  (`CREATE ROLE grafana_ro LOGIN PASSWORD :'pw' NOSUPERUSER NOCREATEDB
  NOCREATEROLE`; password from `GRAFANA_DB_PASSWORD` in the environment of
  the migration process, host `.env`), `GRANT CONNECT`, `GRANT USAGE ON SCHEMA
  public`, `GRANT SELECT` on the views and on nothing else. Downgrade revokes
  and drops. This is the db's first non-superuser role; the F-014 fix reuses
  the pattern.
- **`grafana` compose service**: `grafana/grafana-oss:<pinned tag>`, published
  on `${DEV_SSH_BIND:-127.0.0.1}:3000:3000` (same rule and default as sshd:
  a missing variable exposes nothing), `restart: unless-stopped`, env
  `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`,
  `GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:?}`,
  `GF_USERS_ALLOW_SIGN_UP=false`, `GF_ANALYTICS_REPORTING_ENABLED=false`;
  volumes `grafana:/var/lib/grafana` (its own state, not backed up:
  everything that matters is provisioned) and
  `./ops/grafana/provisioning:/etc/grafana/provisioning:ro`,
  `./ops/grafana/dashboards:/var/lib/grafana/dashboards:ro`; `depends_on: db
  healthy`. Datasource `evcrawl` → `db:5432`, user `grafana_ro`, password
  `${GRAFANA_DB_PASSWORD}` via `secureJsonData`, `sslmode: disable` (compose
  network only, same acceptance as the audit's "postgres ssl" line).
  Dashboards provider: `allowUiUpdates: false` (dashboards change by PR),
  `updateIntervalSeconds: 30`. `.env.example` gains the two variables with
  comments; the entrypoint does not export them to members.
- **Four dashboards** (`ops/grafana/dashboards/*.json`, uid fixed, timezone
  UTC, refresh 1 m), each panel a query on a view only:
  1. *Freshness*: state timeline / heatmap source × city coloured by verdict,
     age vs SLA bars, table of breaches (`verdict <> 'fresh'`), scheduler
     heartbeat age stat.
  2. *Crawl runs*: runs and errors per hour per source, mean seconds, skipped
     ratio, created/merged/removed per day, last error lines table.
  3. *Catalog*: events active/upcoming by UF and city, by category, review
     queue, removed per day, listings per source.
  4. *Geocoder*: venues without location by city, cache hit ratio, negative
     cache size, unresolved venues table (name, city, first seen).
- **status.py on the views**: `LAST_RUNS_SQL`/`TOTALS_SQL` replaced by
  `select ... from v_source_city_freshness` and `v_scheduler`; the verdict
  comes from the view; `--hours` stays as an override of `sla_seconds`;
  `--city` prints the per-city table instead of the per-source roll-up;
  inside mode reports `scheduler: up/down (heartbeat 42s)` instead of "not
  visible". Exit codes unchanged; the JSON shape gains `cities` and
  `scheduler`. `release.sh`'s last step prints the Grafana URL when
  `DEV_SSH_BIND` is set.
- **Tests**: `tests/test_grafana_queries.py` loads every dashboard JSON,
  extracts each panel's `rawSql`, and runs `EXPLAIN` against the scratch
  database after `alembic upgrade head` (skipped without
  `EVCRAWL_TEST_DATABASE_URL`, like the other DB tests); a broken view or a
  renamed column fails CI, not the dashboard at 3 a.m. `tests/test_db_models.py`
  gains a smoke SELECT per view.

## 4. Tasks

Each task: its own branch (`feat/monitoring-<topic>`), gates green in the
`dev` container, PR into `main`.

1. **Views + heartbeat table in migration 0003** — the six views, the
   `scheduler_heartbeat` table, the `grafana_ro` role, the `VIEWS` docstring
   in `db/models.py`; coordinated with spec 1 task 3 and spec 3 task 1
   (one migration file; whichever PR is first carries it, the others add
   to it before merge, and the spec says so in the PR). Done: upgrade and
   downgrade on `<login>_scratch`; the role can `SELECT` from every view and
   nothing else (`tests/test_db_models.py` asserts a denied `SELECT` on
   `events` and a denied `INSERT`).
   **Landed 2026-09-23** (branch `feat/monitoring-views`, migration `0005`,
   since `0003` was merged before this spec started): the six views,
   `scheduler_heartbeat` (CHECK `id = 1`, model `SchedulerHeartbeat`),
   `grafana_ro`, and `VIEWS` in `db/models.py` (name → columns; a test
   asserts every view returns exactly those). Deviations from §3, all
   additive: `v_source_city_last_run` also lists an enabled source with no
   crawl row (nulls, so the `never` verdict exists in SQL) and carries
   `running_since`; `v_source_city_freshness` uses the AGENTS.md §9 six
   hours as a constant `sla_seconds` until spec 3 task 1 replaces the view
   (`create or replace`) to join `sla`; `v_crawl_runs_hourly` groups by
   `kind` too, so refresh runs do not pollute the cycle counts;
   `v_geocode_health` adds the active-event coverage the `geocode` SLA
   needs (`active_events`, `active_events_located`, the ratio). The role's
   password lives in `evcrawl.db.roles`: the migration sets it only for a
   new or `NOLOGIN` role (roles are cluster-wide, a scratch migrate must not
   clobber the shared db's), `evcrawl migrate` re-applies
   `GRAFANA_DB_PASSWORD` after every upgrade so a later release turns login
   on or rotates it, and the downgrade drops the role only when no other
   database still grants it. Verified on scratch: upgrade, `downgrade -1`,
   a real `psql` login as `grafana_ro` reading a view and denied on
   `events`, the migrate-then-set-password path, rotation, and the role
   surviving a downgrade while a second scratch database still granted it.
2. **Heartbeat job + status.py on the views** — the `heartbeat` scheduler
   job, `status.py` rewritten on the views with `--city` and the scheduler
   line, `tests/test_scheduler.py` extended. Done: inside the container,
   `python3 ops/host/status.py` says whether the scheduler is up; on the
   host the JSON output has `cities` and `scheduler`.
3. **Grafana service + provisioning** — compose service, `ops/grafana/`
   datasource and provider YAML, `.env.example` variables, `usher.service`
   ExecStart/ExecStop gain `grafana`, `ops/host/README.md` section
   "Dashboards" (URL, anonymous viewer, admin password location, that
   dashboards change by PR), commit says "rebuild required" (compose
   change). Done: on the host, `docker compose up -d grafana` (owner) serves
   the login page on the tailnet address only (`ss -ltn` shows no `0.0.0.0:3000`);
   the datasource "Save & test" passes.
4. **The four dashboards** — JSON under `ops/grafana/dashboards/`, the
   `EXPLAIN` test, a screenshot of each in the PR. Done: every panel renders
   on the host with the live db; `tests/test_grafana_queries.py` covers
   every panel.
5. **Close-out** — `release.sh` prints the Grafana URL; a note for the next
   `cerberus` audit in `docs/security/README.md` "Scope notes" (new port on
   the tailnet address, new read-only role, anonymous viewer accepted by the
   owner); AGENTS.md §3 layout gains `ops/grafana/`; §5 host commands gain
   `grafana`. Done: §5 below holds.

## 5. Acceptance for the spec

- All four gates green in the `dev` container and in CI; the `EXPLAIN` test
  runs in CI's dev-image job against its scratch database.
- The four dashboards render over the tailnet without a login; nothing
  listens on `0.0.0.0:3000`.
- `status.py` inside the workspace reports scheduler up/down from the
  heartbeat; host mode and Grafana show the same verdict for every source ×
  city (spot check in the PR).
- `grafana_ro` cannot read `events`, `raw_snapshots` or any table, and cannot
  write anything.
- No personal data in any panel (venue and organizer names are public event
  data; descriptions and raw snapshots are never selected).

## 6. Ask the owner before

Publishing Grafana on any address other than `DEV_SSH_BIND`; enabling Grafana
plugins or sign-up; giving members editor accounts; adding Prometheus, an
exporter or a metrics endpoint; materialising a view; backing up the
`grafana` volume.
