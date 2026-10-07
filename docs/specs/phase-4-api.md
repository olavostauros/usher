# Phase 4 build spec: the read-only events API for the Usher PWA

Status: tasks 1-5 done (task 5 landed 2026-09-25, PR #65, ADR 0015; the
`events_upcoming` fix is ADR 0016, migration `0010`). Open: the security
re-audit of the public surface that task 5 asks for, and task 6, the phase
close, after the Google Cloud move (`DEPLOY.md`). Written 2026-09-24. The ADR is
[0014](../decisions/0014-events-api-in-usher.md). The contract the PWA team
reads is [`docs/api/README.md`](../api/README.md); this spec is how we build
it. Do the tasks in order, one branch and one PR per task. AGENTS.md rules
apply throughout.

## 1. Goal

The Usher PWA gets "events near me" and event details over HTTP from a
read-only API served by this repository's `runtime` image. The API reads the
crawl database only through a set of `api_*` views, logged in as a role
(`api_ro`) that can do nothing else. The OpenAPI document is committed, so
the PWA can build against a fixed contract. The API is live on the tailnet
for PWA development after task 4, and it is public only after task 5 and the
owner's decision on exposure.

Out of scope: anything the PWA writes (accounts, favourites, profiles,
check-ins), authentication of end users, ticket sales or checkout
(AGENTS.md §1), search ranking or full-text relevance beyond a plain title
match, push notifications, and image hosting or resizing (§10: URLs only).
The PWA keeps the user's profile. The API takes filters, never identities.

## 2. Facts (2026-09-24; verify before relying on them)

- **The catalogue** (`ops/host/status.py`, 2026-09-24 19:05 UTC): 10,795
  active events, 2,117 in the next 7 days, 171 under dedupe review, 4,705
  `removed_from_source`, 15,526 listings, 7,505 venues, 21 venues without a
  location. There are 33 city rows: 32 in the registry, plus `-` for runs
  that cover every city.
- **Data quality the PWA will see:** in the same status, Sympla (by far the
  largest source) reports errors in 16 of 33 cities, TicketBolt in 4 and Zig
  in São Paulo. The API serves whatever the crawl left behind. Freshness
  spec 3 (`phase-3-freshness.md`) is the work that makes that measurable and
  alerting; it is paused, not dropped (§3).
- **The query the PWA needs already exists in the schema.** `venues.location`
  is `geography(Point, 4326)` with the GiST index `ix_venues_location`.
  `events.starts_at` and `events.status` are indexed. "Active" is
  `status in ('scheduled', 'updated')`, the same definition the monitoring
  views use (migration `0005`). Radius queries must use `ST_DWithin`
  (AGENTS.md §7).
- **Event ids are stable.** Events are never deleted, and a merge links a
  listing to an existing event instead of renumbering (ADR 0010, ADR 0012).
  `events.id` can be the public id.
- **Attribution and the buy link:** every event has one or more
  `source_listings.source_url`. We do not sell tickets, so "buy" in the PWA is
  a link to the source (§10: keep attribution).
- **Public fields only** (§10): title, description (contacts scrubbed,
  ADR 0011), dates, timezone, venue name, address and point, organizer name
  and URL, category, price lots, image URL. `raw_snapshots`,
  `event_versions.content`, `geocode_cache.response`, `crawl_runs.error_detail`
  and fingerprints never leave the database through the API.
- **The monitoring pattern to copy:** migration `0005` created `grafana_ro`
  (`SELECT` on the views only). `db/roles.py` manages its password outside
  alembic. `VIEWS` in `db/models.py` is the column contract, and
  `tests/test_grafana_queries.py` runs every panel's SQL as that role.
- **The host exposes nothing publicly today.** sshd (2222) and Grafana (3000)
  are published on `DEV_SSH_BIND` (the Tailscale IP) only, and
  `ops/host/README.md` forbids `0.0.0.0`. The whole stack is the owner's
  personal computer, with best-effort availability (AGENTS.md §4).
- **Dependencies:** there is no web framework in `pyproject.toml` today.
  Pydantic v2, SQLAlchemy 2 and GeoAlchemy2 are already there.
- **Migrations:** `0001`-`0006` are merged, and `0007` is F-023's bounds on
  `grafana_ro` (`docs/security/reports/2026-09-24-usher-stack.md`). The
  freshness spec's `sla` table and this spec's views take the next free
  numbers, whichever lands first.

## 3. Design decisions

Proposed 2026-09-24 with the scope PR; the reviewer confirms them by merging
it. A change later is an edit to this section plus an ADR.

- **The API lives in this repository** (ADR 0014), as the package
  `src/evcrawl/api/`, in the same `runtime` image, started by
  `evcrawl api`. AGENTS.md §1's "does not define the PWA's API layer" becomes
  "defines the read-only events API the PWA consumes; nothing that writes".
- **Framework: FastAPI on uvicorn.** It uses the Pydantic v2 models we
  already write, generates the OpenAPI document, and is async. The only
  dependencies added are `fastapi` and `uvicorn[standard]` (plus `httpx`,
  already present, for the test client). The DB access is SQLAlchemy Core
  with `text()` SQL on the views, run through `run_in_threadpool` on the
  existing sync engine (psycopg 3). A second, async driver is not worth it at
  this size.
- **Read-only by construction.** There is a new role, `api_ro`
  (`nosuperuser nocreatedb nocreaterole noinherit`), with `SELECT` on the
  `api_*` views and nothing else: no tables, no monitoring views. Its password
  is `EVCRAWL_API_DB_PASSWORD`, handled by `db/roles.py` exactly as
  `grafana_ro`'s is (the migration creates it `NOLOGIN` without a password,
  and `evcrawl migrate` applies the password). The API process connects with
  `EVCRAWL_API_DATABASE_URL`, never with `DATABASE_URL`. Every session
  also sets `default_transaction_read_only = on` and
  `statement_timeout = 3s`.
- **The views are the contract**, like the monitoring ones. They are
  `API_VIEWS` in `db/models.py` (name → columns), with a test that asserts
  them:
  - `api_events`: one row per active, not-yet-ended event
    (`status in ('scheduled','updated') and coalesce(ends_at, starts_at) >=
    now() - interval '6 hours'`). Columns: `id, title, title_normalized,
    description, starts_at, ends_at, timezone, is_online, image_url,
    category, category_name, venue_id, venue_name, venue_address,
    venue_city, venue_uf, location` (geography), `lat, lon,
    organizer_name, organizer_url, price_min, price_max, currency,
    is_free, cities` (text[] of registry slugs from the event's live
    listings), `sources` (jsonb `[{source, url}]` from the live listings),
    `updated_at`. Events under `dedupe_review` are included: they are real
    events, possibly listed twice. The PWA shows them.
  - `api_event_prices`: `event_id, name, amount, currency, position`.
  - `api_event_status`: `id, status` for every event, so the detail endpoint
    can tell 404 (never existed) from 410 (no longer active). Nothing else
    about an inactive event leaves the database.
  - `api_categories`: `slug, name, events_upcoming`.
  - `api_city_counts`: `slug, events_upcoming` (the unnested `cities` of
    `api_events`). The city list itself (name, UF, timezone) comes from
    `cities.py` in the app, so there is no new table and the registry stays
    the only source of truth.
- **Endpoints, version 1** (full shapes in `docs/api/README.md`). All are
  `GET`, all return JSON, and there is no auth in v1:
  - `/v1/events`: `lat`, `lon`, `radius_km` (default 25, max 100), `from`,
    `to` (ISO date or datetime; default now → now + 30 days, max span 180 days),
    `city` (registry slug), `category` (repeatable), `free`, `online`
    (`include` | `only` | `exclude`, default `exclude` when `lat`/`lon` are
    given, otherwise `include`), `q` (a title match on `title_normalized`,
    unaccented and lowercased like the pipeline does it), `sort` (`distance`
    when `lat`/`lon` are given, else `starts_at`), `limit` (default 20, max
    100), `cursor`. Either `lat`+`lon` or `city` is required. A missing or
    partial location is a 422, not a national dump.
  - `/v1/events/{id}`: one event plus its price lots and every source link.
    It returns 404 for unknown ids, and **410** with `{status}` for events
    that are no longer active (cancelled, ended, removed), so a PWA
    bookmark can say why.
  - `/v1/categories`, `/v1/cities`: the lists for the filters.
  - `/healthz`: `{"ok": true, "git_sha", "db": "ok"}`, which checks the db
    with `select 1` as `api_ro`. It reports catalogue freshness only after
    freshness spec 3 lands.
  - `/v1/openapi.json`, plus the docs UI at `/v1/docs` (tailnet only; see
    exposure).
- **Pagination** is keyset: the cursor is an opaque base64 of the last row's
  sort key and id (`(distance_m, id)` or `(starts_at, id)`). There are no
  offsets and no total count, and a request never returns more than 100 rows.
- **Response hygiene.** Times are ISO 8601 with offset in UTC, plus the
  event's IANA `timezone` for local display. Money is a decimal string
  (`"45.00"`), never a float (§7). Coordinates have 6 decimals. Free means
  `is_free = true` with `price_min = "0.00"`. `null` means unknown; an
  unknown value is never replaced with a guess.
- **Caching:** `Cache-Control: public, max-age=300` on lists, 900 on detail,
  categories and cities, plus a weak `ETag` from `max(updated_at)` of the
  rows returned. The crawl is 6-hourly (hourly within 48 h), so five minutes
  of staleness is invisible.
- **CORS:** the allowlist comes from `EVCRAWL_API_CORS_ORIGINS`
  (comma-separated; empty means none). Only `GET` and no credentials are
  allowed.
- **Abuse limits in the app:** the parameter caps above, the 3 s statement
  timeout, and a per-client token bucket in process (60 req/min per IP, from
  `X-Forwarded-For` only when `EVCRAWL_API_TRUST_PROXY=1`). There is no Redis
  dependency in v1. Rate limiting at the edge is part of exposure.
- **Logging:** one structlog line per request: method, route template (not
  the raw query string), status, duration, row count. There are **no client
  IPs and no coordinates in logs** (LGPD: a user's location is personal
  data). The rate limiter keeps IPs in memory only, and only for its window.
- **The compose service `api`** runs the `runtime` image with
  `command: ["api"]` and `restart: unless-stopped`. It is published on
  `${DEV_SSH_BIND:-127.0.0.1}:8000` only, like sshd and Grafana, and has a
  healthcheck on `/healthz`. `release.sh` starts it after the migration and
  smoke, and `status.py` shows it. It is the same image as the scheduler: one
  release, one git SHA.
- **The PWA team's contract:** `docs/api/README.md` (prose and examples) and
  `docs/api/openapi.json` (generated). A test fails when the app's schema
  and the committed file differ, so a contract change is always a visible
  diff in a PR. Breaking changes go to `/v2`, and `/v1` stays until the PWA
  has moved.
- **Recurring sessions collapse in lists** (owner's decision, 2026-09-24).
  Some sources list each session of a recurring event as its own event. On
  the shared db, the guided tour of the Museu da Estrada de Ferro
  Madeira-Mamoré is about 800 of Porto Velho's 915 upcoming events: one per
  hourly slot (`17h (horário local) | VISITA GUIADA - …`) for about 80
  days. `/v1/events` groups events that have the same venue and the same
  title once a leading time-of-day prefix is removed. It returns the next
  session, with a `sessions` count and that session's id. `/v1/events/{id}`
  still serves every session. Adding the field is not a breaking change.
  The counts in `/v1/categories` and `/v1/cities` stayed per event until
  2026-09-25, when the PWA showed the city count next to a list a fraction
  its size; ADR 0016 and migration `0010` make `events_upcoming` the number
  of rows the list returns over its widest window, a series once. The
  alternative, series detection in the pipeline (a schema change to a core
  table), waits until a consumer other than this API needs it.

**Paused:** freshness spec 3 (`phase-3-freshness.md`) waits until this
spec's task 4 has landed. Then the owner picks the order between it and task
5. Its §3 is unchanged, and `/healthz` will grow a `fresh` field when
`evcrawl health` exists.

**Decided 2026-09-25 (task 5, ADR 0015):** (1) option (a), a named
Cloudflare Tunnel from this host, `https://api.eventolivre.com`; (2) the
PWA calls the API from the browser, and the only CORS origin is
`https://eventolivre.com`; (3) no API key in v1. The questions as they were
asked:

**Open, the owner's to decide before task 5:**

1. **How the PWA reaches the API from the internet.** The options, with the
   trade-offs:
   - (a) **Tunnel from this host** (Cloudflare Tunnel or Tailscale Funnel)
     to the `api` service. Nothing gets published on the host's interfaces,
     and TLS and edge rate limiting come from the tunnel provider. The catch
     is that the PWA goes down whenever this personal computer does.
   - (b) **Hosted read replica or export.** A managed Postgres+PostGIS
     receives the `api_*` rows (logical replication or a periodic dump of
     the views), and the same image runs the API next to it. Availability
     stops depending on the host, but it adds an external service (§11 "ask
     before") and a sync job.
   - (c) **Tailnet only** until the PWA leaves development, then (a) or (b).
   - Recommendation: (c) now, then (a) for a beta, then (b) when the PWA has
     real users.
2. **The PWA's origins** for CORS, and whether the PWA calls the API from
   the browser directly or through its own server (the latter hides the API
   and needs no CORS).
3. **Whether a public v1 needs an API key** (a static key per client in the
   host `.env`). The data is public, so the proposal is no key, with limits
   at the edge.

## 4. Tasks

Each task gets its own branch (`feat/api-<topic>`), gates green in the `dev`
container, and a PR into `main`. DB-backed tests run on `<login>_scratch` and
in CI (`EVCRAWL_TEST_DATABASE_URL`).

1. **Views and role.** One migration (the next free number): the five
   `api_*` views, `api_ro`
   with `SELECT` on them only, and `roles.py` generalised to both roles
   (`EVCRAWL_API_DB_PASSWORD` in `.env.example`, host-only comment).
   `API_VIEWS` in `db/models.py`.
   Done when: a test asserts the columns; a test logs in as `api_ro` and
   gets `permission denied` on `events`, `raw_snapshots` and
   `v_scheduler` but rows from every `api_*` view; `EXPLAIN` of the radius
   query on seeded rows uses `ix_venues_location`.
2. **App skeleton.** `fastapi` and `uvicorn[standard]` added with `uv add`
   (the commit says "rebuild required"). `src/evcrawl/api/`
   (`app.py` factory, `db.py` read-only session, `schemas.py` Pydantic
   response models, `routes/`), `evcrawl api [--host] [--port]`, the
   settings (`EVCRAWL_API_DATABASE_URL`, `EVCRAWL_API_CORS_ORIGINS`,
   `EVCRAWL_API_TRUST_PROXY`), `/healthz`, `/v1/categories`, `/v1/cities`,
   request logging, the OpenAPI snapshot test and the committed
   `docs/api/openapi.json`. Done when: `uv run evcrawl api` in the workspace
   answers `/healthz` against scratch, and mypy is strict-clean.
3. **Events endpoints.** `/v1/events` (every parameter in §3, keyset
   cursor, both sorts, recurring sessions collapsed), `/v1/events/{id}` (404
   and 410), caching headers, parameter caps, the rate limiter, and the
   `sessions` field in `docs/api/README.md`. Done when:
   `tests/test_api_events.py` seeds scratch with events around two cities
   (online, free, paid, ended, removed, under review, venue without a
   location, one recurring event with several sessions) and asserts the radius
   boundary (an event just inside and just outside `radius_km`), ordering,
   the cursor round trip with no duplicates or gaps, the 422s, 404, 410 and
   decimal-string prices. Also `EXPLAIN` on the three query shapes (radius,
   city, radius + category) with no seq scan on `venues` or `events` at
   seeded scale, and on the shared db a latency spot check (p95 < 200 ms for
   a 25 km radius around São Paulo), recorded in the PR.
4. **Compose service and release wiring.** The `api` service in
   `docker-compose.yml` (tailnet bind, healthcheck, an explicit `environment:` list and no
   `env_file`, F-026); `release.sh`
   starts it and prints its URL next to Grafana's; `status.py` shows it; the
   "API" section in `ops/host/README.md`; the scope note in
   `docs/security/README.md` (a new listening port, a new role). The commit
   says "rebuild required". Done when the owner has run `release.sh`, and
   `curl http://<DEV_SSH_BIND>:8000/healthz` answers from a member's
   machine on the tailnet while `ss -ltn` shows 8000 on the Tailscale IP
   only. The PWA team can start against it.
5. **Public exposure.** Option (a), ADR 0015. It implements the chosen
   option, edge rate limiting, the CORS origins, and a re-audit
   (`cerberus`) of the new surface before announcement.
6. **Phase close.** AGENTS.md §12 gets the Phase 4 paragraph, CLAUDE.md
   "Current work" moves on, and this spec gets its "Outcome" paragraph.
   `docs/api/README.md` loses its "draft" banner.

## 5. Acceptance

- All four gates are green in the `dev` container and in CI, including the
  DB-backed API tests.
- `api_ro` can read the `api_*` views and nothing else (a test proves it).
  The API process never holds `DATABASE_URL`.
- The committed `docs/api/openapi.json` equals the app's schema.
- No personal data in responses (organizer contacts are already scrubbed,
  and there is no attendee data) and none in logs (no IPs, no coordinates).
- The PWA's "events near me" works end to end against the tailnet API
  (task 4). After task 5, it works from the public origin, over TLS.
- The radius query uses the GiST index, and p95 is under 200 ms on the
  shared db.

## 6. Ask the owner before

Adding any dependency beyond `fastapi` and `uvicorn[standard]`; publishing
any port on anything but `DEV_SSH_BIND`; any public exposure, tunnel or
hosted service (§3 "Open"); any write endpoint or storing anything about PWA
users; changing a view column in `API_VIEWS` after the PWA depends on it
(that is a `/v2`); resuming freshness spec 3 ahead of task 5.
