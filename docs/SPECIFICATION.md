# SPECIFICATION.md — what evcrawl is, and what is left to build

The one specification of this repository: the system as built (§1-§7) and
the work still open (§8). Read it with AGENTS.md, which holds the rules;
this file holds the behaviour. The decisions behind each part are the ADRs
in `docs/decisions/`, cited per section; where an ADR and this file differ,
the ADR wins and this file is fixed.

It replaces the per-phase build specs that lived in `docs/specs/`
(`phase-1-sympla`, `phase-2-sources`, `phase-3-national`,
`phase-3-monitoring`, `phase-3-freshness`, `phase-4-api`), removed on
2026-10-08. Their recon notes, task lists and outcome paragraphs stay in git:
`git show cdce869:docs/specs/<name>.md`.

## 1. Goal and scope

The Usher PWA answers "events near me" for Brazil through a read-only events
API (§7). This repository keeps the data behind it: it crawls the public
listings of five ticketing platforms (§2) in 32 cities (§4), normalizes,
geocodes and deduplicates them (§3) into PostgreSQL + PostGIS (§5), watches
its own freshness (§6, §8.1), and serves the result (§7).

Out of scope, always (AGENTS.md §1, §10): ticket sales, payments,
checkout; any frontend; anything the PWA writes or stores about its users;
attendee or buyer data; image files (URLs only); evading a source's
blocking.

## 2. Sources

One adapter per platform in `src/evcrawl/sources/`, `discover()` +
`fetch()` returning `EventRecord`s, never touching the DB; `REGISTRY` in
`sources/__init__.py`. Order of preference: public JSON API → embedded JSON →
HTML → (never, so far) a headless browser. The User-Agent is always the
project's own; per-host extra headers are declared on the adapter and sent to
that host only. Adding or changing one follows the `add-source-adapter`
skill and AGENTS.md §6.

| Source | How it is read | Extra header | ADR |
|---|---|---|---|
| Sympla | city listing pages (RSC chunks), the server-rendered event page, the `event-bff` tickets endpoint for lots | — | 0003, 0004 |
| uTicket | JSON API: `eventsearch`, `eventinfo`, `tickettype` (real lots) | `Referer: https://uticket.com.br/` on `api.uticket.com.br` | 0006 |
| BlueTicket | JSON API: state-wide listing filtered by city, `detail`, `products/sectors` | — | 0007 |
| TicketBolt | the Convex `getPublicEventsList` query and the event page's RSC flight payload | `RSC: 1` on `www.ticketbolt.com` | 0008 |
| Zig | static `new-events.json` feed + `__NEXT_DATA__` (`ai-train=no` noted) | — | 0009 |

Known state: TicketBolt sits behind a Queue-it waiting room (#55), so the
release smoke fails on it; pausing the source is the owner's call.

## 3. Pipeline

`pipeline/crawl.py`, one run: discover → fetch → normalize → geocode →
dedupe → upsert, and one `crawl_runs` row per run (start, end, city, kind,
the six counts, errors).

- **Fetch** (`fetch.py`): one process-wide `Fetcher`; `robots.txt` honoured;
  per-source rate limit (default 1 req/s), exponential backoff on 429/5xx,
  at most 5 retries.
- **Normalize** (`normalize.py`, AGENTS.md §8): titles trimmed with a
  `title_normalized` for matching; categories mapped to the internal
  taxonomy, unknown ones to `uncategorized` and logged; prices
  `numeric(12,2)` + `currency`, free = a lot of `0`; times `timestamptz` UTC
  plus an IANA `timezone`. Contact strings are scrubbed from descriptions
  and organizer contacts are not stored (ADR 0011).
- **Geocode** (`geocode.py`, ADR 0017, 0018): source coordinates first;
  otherwise the public Nominatim at 1 req/s, query chain address without CEP
  → street + city + state → venue + city + state, clipped to the metro
  area, cached in `geocode_cache` by normalized address. Failures are never
  cached as misses. No self-hosted geocoder.
- **Dedupe** (`dedupe.py`, ADR 0010): a new listing with exactly one
  confident cross-source candidate (same normalized title, start ± 2 h,
  venue within 300 m, not under review) links to that event and writes an
  `event_versions` row; the oldest listing's content wins, empty fields are
  filled from any source; anything ambiguous stays separate with
  `dedupe_review = true`.
- **Upsert** (`upsert.py`): events are never deleted; status moves through
  `scheduled`, `updated`, `cancelled`, `ended`, `removed_from_source`, each
  change with an `event_versions` row.

## 4. National coverage and scheduling

- **Cities** (`src/evcrawl/cities.py`): 32 rows, the six cities of Greater
  Vitória and the 26 other state capitals, each with its UF and zone (UF →
  IANA timezone table). `--city` takes a slug, the old `-es` form or the name.
  `enabled = False` pauses a city.
- **Scheduler** (`scheduler.py`, APScheduler, UTC), standing jobs per source:
  `cycle:<source>` every 6 h walks every enabled city on one adapter;
  `refresh:<source>` hourly re-checks events starting within 48 h; plus
  `heartbeat` every minute, which upserts `scheduler_heartbeat`. All jobs
  `coalesce` and run one instance at a time. Only one scheduler crawls the
  production database (AGENTS.md §13).
- **Conditional fetch** (ADR 0013): a listing whose fingerprint is unchanged
  and that does not start within 48 h is skipped (`skipped` count).
- **Removal per listing** (ADR 0012): a listing missing from its source's
  city listing gets `removed_at`; an event becomes `removed_from_source`
  only when every one of its listings is gone. A listing whose own page the
  source answers 404 or 410 for is removed the same way, by a crawl or the
  hourly refresh, and counts as `gone`, not as an error, while the run
  fetched more pages than it found gone (ADR 0022).
- **Freshness targets** (AGENTS.md §9): every active source × city re-crawled
  at least every 6 h; events within 48 h re-checked hourly. §8.1 turns them
  into checked SLAs.
- **Still to measure** (the owner's, from `crawl_runs` on production): the
  duration of one full cycle per source over the 32 cities against the 6 h
  window. The first full cycle was expected near 9 h for Sympla (no
  fingerprints yet); record the measured figures here.

## 5. Data model

`src/evcrawl/db/models.py` is the single source of truth; every change is an
Alembic migration (AGENTS.md §7). Core tables: `events`, `event_versions`,
`venues` (`location geography(Point, 4326)`, GiST; radius queries use
`ST_DWithin`), `organizers`, `categories`, `price_lots`, `sources`,
`source_listings` (one per source × event, keeps `source_url`),
`raw_snapshots` (compressed payloads), `geocode_cache`, `crawl_runs`,
`scheduler_heartbeat`.

| Migration | What it added |
|---|---|
| `0001` | `crawl_runs` |
| `0002` | the core tables |
| `0003` | run metadata per city (`city`, `kind`: `crawl` / `refresh` / `manual`), listing city, removal and fingerprint |
| `0004` | server defaults for the run counts |
| `0005` | the six monitoring views, `scheduler_heartbeat`, `grafana_ro` |
| `0006` | three more dashboard views |
| `0007` | per-session bounds on `grafana_ro` (F-023) |
| `0008` | the five `api_*` views and `api_ro` |
| `0009` | index on `events.venue_id` for radius joins |
| `0010` | `events_upcoming` counts what `/v1/events` lists (ADR 0016) |
| `0011` | next free number: the `sla` table of §8.1 |

**Roles** (`db/roles.py`, set by `evcrawl migrate`): `evcrawl` is the only
superuser (the release container's); `team` is the members' role (rows in
`public`, their own schema and `_scratch` databases); `grafana_ro` may
`SELECT` the monitoring views only, with bounded sessions; `api_ro` may
`SELECT` the `api_*` views only.

**View contracts**: `VIEWS` and `API_VIEWS` in `models.py` list every
column and tests assert them. Renaming a monitoring column breaks
`status.py`, Grafana and `evcrawl health`; renaming an API column is a `/v2`.

## 6. Monitoring

Everything reads views, never tables (AGENTS.md §7):
`v_source_city_last_run`, `v_source_city_freshness`, `v_crawl_runs_hourly`,
`v_catalog_by_city`, `v_geocode_health`, `v_scheduler` (`0005`);
`v_catalog_by_category`, `v_listings_by_source`, `v_venues_unresolved`
(`0006`).

- **`ops/host/status.py`**: containers, the scheduler up/down from the
  heartbeat (down after 5 min), freshness per source × city; `--city`,
  `--json`; `--from <file>` reads the VM's status snapshot.
- **Grafana** (the `grafana` service, `grafana/grafana:13.0.10`,
  digest-pinned): tailnet only, anonymous Viewer, datasource `evcrawl` as
  `grafana_ro`, UI edits off; four dashboards in `ops/grafana/dashboards/`
  (`freshness`, `crawl-runs`, `catalog`, `geocoder`), every panel on a view.
  `tests/test_grafana_queries.py` runs every panel's SQL as `grafana_ro`.
- **No personal data in any panel**: descriptions and snapshots are never
  selected.
- **Open (owner):** the host checklist: the four dashboards render over the
  tailnet without a login, nothing listens on `0.0.0.0:3000`, `status.py` and
  Grafana agree on every source × city.

## 7. Events API

`src/evcrawl/api/`, FastAPI on uvicorn, `evcrawl api`, the `api` compose
service (same `runtime` image as the scheduler). ADRs 0014 (it lives here),
0015 (public through the Cloudflare Tunnel), 0016 (counts). The PWA team's
contract is `docs/api/README.md` plus the generated `docs/api/openapi.json`;
a test fails when the app's schema and the committed file differ.

- **Endpoints, `/v1`, `GET` only:** `/healthz`; `/v1/events` (radius from
  `lat`/`lon`/`radius_km` with `ST_DWithin`, or `city`; dates, category,
  free, sort; keyset cursor; recurring sessions collapsed in lists);
  `/v1/events/{id}` (404 never existed, 410 no longer active);
  `/v1/categories`; `/v1/cities`.
- **Read-only by construction:** the process connects as `api_ro` through
  `EVCRAWL_API_DATABASE_URL` and never holds `DATABASE_URL`.
- **Limits:** parameter caps, a 3 s statement timeout, an in-process token
  bucket of 60 req/min per client (`X-Forwarded-For` only with
  `EVCRAWL_API_TRUST_PROXY=1`), Cloudflare rate limiting at the edge.
- **Caching:** `max-age=300` on lists, 900 on detail, weak `ETag`.
- **CORS:** `EVCRAWL_API_CORS_ORIGINS` (`https://eventolivre.com` in
  production), `GET` only, no credentials.
- **Logs:** method, route template, status, duration, row count; never
  client IPs or coordinates (LGPD).
- **Exposure:** `https://api.eventolivre.com` through the tunnel `usher-api`
  (the `cloudflared` service), served from the VM `usher-prod` since
  2026-10-07 (`docs/DEPLOY.md`). The `cerberus` re-audit of the public
  surface passed 2026-10-07.
- **Never without the owner:** a write endpoint, anything stored about PWA
  users, a dependency beyond FastAPI/uvicorn, a port beyond `DEV_SSH_BIND`,
  a change to an `API_VIEWS` column once the PWA depends on it.

## 8. Open work

In the owner's order (AGENTS.md §13): the Google Cloud move first
(`docs/DEPLOY.md` steps 10-11), then the owner orders the two below. Each
task: its own branch, gates green in the `dev` container
(`ops/dev/gates.sh`), PR into `main`.

### 8.1 Freshness SLAs and alerting (Phase 3 close; paused since 2026-09-24)

**Goal.** `evcrawl health` exits 0 when every SLA holds and 1 with a JSON
list of breaches when one does not; the scheduler runs it every 15 minutes
and posts each breach once, and its recovery once, to the team's Discord
channel through a webhook; the `evcrawl` container reports `healthy`. "Is it
crawling?" gets a yes/no answer from the host, the VM and the workspace.
Out of scope: any other alert destination, paging, Grafana alert rules.

It also closes the gap `docs/DEPLOY.md` §2 records: after a Spot restart
the `api` container came up without its network and nothing alerted for
~3 h. Whether the post-boot repair of `docs/DEPLOY.md` §2 option 1 lands
first is the owner's call.

**SLAs** (code first in `src/evcrawl/sla.py`, mirrored by migration `0011`
into a `sla` table that `evcrawl migrate` re-syncs, so the views can join it):

| name | scope | threshold | measured from |
|---|---|---|---|
| `crawl` | source × city | last successful crawl ≤ 6 h old | `v_source_city_last_run` |
| `refresh` | source | last refresh ≤ 1 h old, or no event within 48 h | `crawl_runs` kind `refresh` |
| `removal` | source × city | a listing unseen for two error-free cycles has `removed_at` | `source_listings` vs last runs |
| `heartbeat` | scheduler | `beat_at` ≤ 5 min old | `v_scheduler` |
| `geocode` | global | ≥ 95 % of active events' venues located | `v_geocode_health` |
| `errors` | source | errors / discovered over the last 3 crawl runs < 20 % | `v_crawl_runs_hourly` |

Per-city overrides are rows with a `city`; a capital a source does not list
is not a breach. A threshold change is a change to AGENTS.md §9 and this
table in one commit.

**Design.**
- `evcrawl health` prints `{"ok", "checked_at", "breaches": [{"sla",
  "source", "city", "measure", "threshold", "since", "detail"}],
  "recovered": [...]}`; exit 0/1, 2 when the db is unreachable; `--quiet`
  for the healthcheck; `--notify` posts transitions. Never event content,
  organizer or personal data; `detail` is an age or the first line of
  `error_detail`.
- State in Redis, key `evcrawl:health:state` (breach key → since): a breach
  posts when it enters, a recovery when it leaves. Redis down = a warning
  and no post. The first real use of `REDIS_URL`; no queue.
- `src/evcrawl/notify.py`: one `post(text)` through the shared `Fetcher`
  (`check_robots=False`, bucket `discord`, 1 req/s, 3 retries) to
  `EVCRAWL_ALERT_WEBHOOK_URL`; unset = disabled with one
  `health.notify_disabled` log. One post per run, ≤ 2000 chars, one line per
  transition plus `ops/host/status.py --city <city>` as footer. The URL is a
  secret: host and VM `.env` only, never exported to members by
  `team/entrypoint.sh`.
- Scheduler job `health` every 15 min (`coalesce`, one instance).
  `HEALTHCHECK CMD ["evcrawl", "health", "--quiet"]` in the runtime stage
  (interval 5 min, timeout 30 s, start period 2 min, retries 2), mirrored in
  `docker-compose.yml`; `status.py` shows container health; `release.sh`
  waits up to 3 min for `healthy`.
- Runbook "When an alert fires" in `ops/host/README.md`: `status.py --city`,
  the Freshness dashboard, then the usual causes: a source answering 4xx/5xx
  or blocking us (stop, tell the owner, AGENTS.md §10); Nominatim unreachable
  (nothing to restart, ADR 0018); a wedged scheduler (owner restarts).

**Tasks.**
1. SLA module, migration `0011`, `v_source_city_freshness` replaced so its
   `sla_seconds` and `verdict` come from the table. Done: unit tests on the
   rows; on scratch, changing a threshold in code and migrating changes the
   verdict.
2. `evcrawl health`: one pure evaluator per SLA over view rows, JSON,
   exit codes, `--quiet`. Done: `tests/test_health.py` seeds a stale city, a
   stale heartbeat and a healthy source and asserts breaches and exit code.
3. Notifier, Redis state, `--notify`, the `health` job,
   `EVCRAWL_ALERT_WEBHOOK_URL` in `config.py` and `.env.example`. Tests with
   `httpx.MockTransport` and `fakeredis` (dev group, rebuild required):
   first breach posts once, the second run nothing, recovery once, unset
   webhook nothing. Done: the owner's disable-a-source drill on production
   gives exactly two messages.
4. Healthcheck, `status.py` health column, `release.sh` wait, the runbook
   (rebuild required). Done: `evcrawl` shows `healthy`; a stopped db makes it
   `unhealthy` within two intervals.
5. Phase close: AGENTS.md §9 lists the SLAs, §12 gets "Phase 3 (done)", §13
   moves on; this section collapses into §4 and §6.

**Ask the owner before:** another alert destination; alert content naming
people or events; changing a threshold; any dependency beyond `fakeredis`;
running the drill.

### 8.2 Phase 4 close

AGENTS.md §12 gets the Phase 4 paragraph, §13 moves on, and
`docs/api/README.md` loses its "draft" banner. Acceptance still to confirm:
the PWA's "events near me" works end to end from the public origin; p95 of a
25 km radius query under 200 ms on production, recorded.

## 9. Keeping this file

- New work gets a subsection of §8 with a goal, decisions, tasks with a
  "Done:" line each, and an "Ask the owner before". Nothing goes back into
  `docs/specs/`.
- When the work closes, its outcome is folded into §1-§7 in the same PR
  and the §8 subsection is deleted; the detail stays in git and the ADRs.
- Recon facts for a new source go into its ADR (the `add-source-adapter`
  skill), not here.
