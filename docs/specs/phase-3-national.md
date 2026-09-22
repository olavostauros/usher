# Phase 3 build spec (1/3): national coverage

Status: ready to build. Written 2026-09-22 from the code, ADRs 0003 and 0006-0010,
and four owner decisions taken the same day (§3 first table). Phase 3 has three
specs; this one runs first because it owns migration `0003`, which the other two
(`phase-3-monitoring.md`, `phase-3-freshness.md`) build on. Execute the tasks in
order, one branch and PR per task. AGENTS.md rules apply throughout; the
`add-source-adapter` skill covers adapter mechanics.

## 1. Goal

`evcrawl crawl all --city sao-paulo --limit 10` works for every state capital
the way `--city vitoria` works today, and one scheduler cycle of every source
covers all 33 cities (27 capitals + the six Greater Vitória cities) inside the
6 h freshness window of AGENTS.md §9, at the unchanged 1 request/second per
source. Listings a source drops are marked `removed_from_source` from the
scheduler, per city, without the churn ADR 0010 describes. Geocoding runs
against a self-hosted Nominatim so the venue volume of 33 cities never depends
on the public instance's goodwill.

Out of scope here: dashboards and the views behind them (spec 2), SLAs and
alerting (spec 3), cities beyond the 33 (a later phase adds registry rows).

## 2. Facts (2026-09-22; verify before relying on them)

**How each source scopes a listing today** (the ADR is the source of truth):

| source | discovery | city/state parameter | hard-coded today |
|---|---|---|---|
| Sympla (ADR 0003) | per-city listing pages, then one event page per event | `eventos/<city-slug>` (`vitoria-es`), page cap 100 | `CITY_SLUGS`, `TIMEZONE` |
| uTicket (ADR 0006) | per-city API search, then `eventinfo` + `tickettype` per event | `eventsearch?city=<name>` (no state filter; namesakes dropped by the `-<UF>` suffix of `place`), 50 × 100 pages | `CITY_NAMES`, `STATE = "ES"`, `TIMEZONE` |
| BlueTicket (ADR 0007) | one state listing, then `detail` + `products/sectors` per event | `events/?state=ES` (no pagination; `city=` ignored server-side) | `STATE = "ES"` is the only listing parameter; `CITY_NAMES`, `TIMEZONE` |
| TicketBolt (ADR 0008) | one national Convex query, then one event page (RSC) per event | `country: "BRA"` only; state and city filtered client-side, 50 pages | `STATE = "ES"`, `CITY_NAMES`, `TIMEZONE`; `_wall_time()` reads the epoch-ms `eventDate` as São Paulo wall time |
| Zig (ADR 0009) | one national static feed (525 kB), then one event page per event | none; state and city filtered client-side | `STATE = "ES"`, `CITY_NAMES`; zone per event when `zoneinfo` knows it |

Every adapter declares `RateLimit(1.0, 5)`. Coordinates: Sympla and uTicket
carry them; BlueTicket sometimes; TicketBolt never; Zig on 1 of 273 feed items.
National coverage therefore multiplies geocoder calls roughly with the number
of venues, not events.

**Pipeline facts that matter at 33 cities:**

- `run_crawl(source, city=...)` (`src/evcrawl/pipeline/crawl.py`) is the crawl
  unit; it is sequential per run. `Fetcher` and adapter are injectable kwargs.
- `run_crawl` builds its own `Fetcher`, so its token buckets are per run: two
  concurrent runs of one source are 2 req/s at that source, and N concurrent
  runs are N streams at Nominatim (`geocode.py` registers its bucket on the
  run's fetcher). The scheduler's staggering comment assumes a shared bucket
  that does not exist.
- The scheduler (`src/evcrawl/scheduler.py`) registers one cron job per
  source × city with `minute = i * 10`: past the sixth city the minutes
  overflow the hour, and a city that takes longer than 10 min overlaps the
  next. `tests/test_scheduler.py` asserts six cities per source, `7 ×
  sources` jobs and the 10-minute stagger.
- `mark_missing` (`src/evcrawl/pipeline/upsert.py`) runs only on a crawl with
  no `--city`, no `--limit`, and no errors (`crawl.py`, `full_crawl`), i.e.
  never from the scheduler. A listing a source drops stays `scheduled`. ADR
  0010's "Consequences" also records the churn when one source drops a merged
  listing while another keeps it.
- Zig's feed cache is per adapter instance and every job builds a new adapter:
  the state/national feeds are fetched once per city job (6× per cycle today,
  33× after this spec without the change in §3).
- `crawl_runs` has `source`, `started_at`, `finished_at`, `discovered`,
  `fetched`, `upserted`, `errors`, `error_detail`: no city, no kind, none of
  `CrawlSummary`'s `created`/`merged`/`versions`/`removed`/`review`.
- The timezone column is `America/Sao_Paulo` everywhere except Zig. Sources
  give offset-aware instants, so `starts_at` is right today; only the zone is
  wrong for events outside the Brasília zone (and TicketBolt's wall time would
  be wrong by 1-2 h there).

**Budget.** 6 h at 1 req/s is 21,600 requests per source per cycle, shared
with the hourly refresh (events within 48 h) and, for uTicket, three requests
per event. Sympla alone lists thousands of events in São Paulo; task 1 measures
it. If the sum over the 33 cities of (listing pages + events) exceeds ~15,000
for any source, the cycle cannot fit without skipping unchanged event pages
(§3, "conditional fetch"). Raising a rate limit is not the answer (AGENTS.md
§10, §11); skipping work is.

**Self-hosted Nominatim.** `mediagis/nominatim:5.3` imports a PBF given by
`PBF_URL` on first start (`IMPORT_STYLE`, `THREADS`, `NOMINATIM_PASSWORD`,
`FREEZE`, optional `REPLICATION_URL`), serves on port 8080, keeps data on a
volume. Geofabrik's `brazil-latest.osm.pbf` is 1.9 GB (2026-09-21). Disk
and import time on the owner's machine are measured in task 7, not assumed.
The public instance's policy is 1 req/s and no bulk use; `.env.example` still
points at it (it answers 403 to a User-Agent mentioning `example.org`).

**Capitals and zones** (seed of the registry; `ibge_code` and per-source keys
are filled in tasks 1-2 from the IBGE municipality table and the sources):

| UF | capital | IANA zone |
|---|---|---|
| AC | Rio Branco | America/Rio_Branco |
| AL | Maceió | America/Maceio |
| AM | Manaus | America/Manaus |
| AP | Macapá | America/Belem |
| BA | Salvador | America/Bahia |
| CE | Fortaleza | America/Fortaleza |
| DF | Brasília | America/Sao_Paulo |
| ES | Vitória (+ Vila Velha, Serra, Cariacica, Viana, Guarapari) | America/Sao_Paulo |
| GO | Goiânia | America/Sao_Paulo |
| MA | São Luís | America/Fortaleza |
| MG | Belo Horizonte | America/Sao_Paulo |
| MS | Campo Grande | America/Campo_Grande |
| MT | Cuiabá | America/Cuiaba |
| PA | Belém | America/Belem |
| PB | João Pessoa | America/Fortaleza |
| PE | Recife | America/Recife |
| PI | Teresina | America/Fortaleza |
| PR | Curitiba | America/Sao_Paulo |
| RJ | Rio de Janeiro | America/Sao_Paulo |
| RN | Natal | America/Fortaleza |
| RO | Porto Velho | America/Porto_Velho |
| RR | Boa Vista | America/Boa_Vista |
| RS | Porto Alegre | America/Sao_Paulo |
| SC | Florianópolis | America/Sao_Paulo |
| SE | Aracaju | America/Maceio |
| SP | São Paulo | America/Sao_Paulo |
| TO | Palmas | America/Araguaina |

Fernando de Noronha (`America/Noronha`) is not a city here; Zig's per-event
zone already handles its events.

## 3. Design decisions (taken; record per-task details in ADRs where noted)

Owner decisions of 2026-09-22 that shape this spec:

| decision | choice |
|---|---|
| first national cut | 27 capitals + the six Greater Vitória cities, through a city registry; more cities are registry rows later |
| geocoder | self-hosted Nominatim for Brazil as a compose service on the host |
| schema | one migration (`0003`) for everything Phase 3 needs on core tables, approved here so the shared db changes once |
| rate limits | unchanged, 1 req/s per source; fit the window by skipping work, never by going faster |

- **City registry** `src/evcrawl/cities.py`: a frozen dataclass `City(slug,
  name, uf, ibge_code, timezone, enabled, keys)` where `keys` maps source name
  → the source's own key for the city (`sympla: "sao-paulo-sp"`, `uticket:
  "São Paulo"`, `blueticket: "São Paulo"`, `ticketbolt: "São Paulo"`, `zig:
  "São Paulo"`); a missing key means the source has no listing there (not an
  error, not a job). `CITIES` is the ordered tuple (Greater Vitória first, then
  capitals by UF); `by_slug()`, `for_source(name)`, `resolve(text)` (accepts
  the slug, the old `-es` forms and the display name). Adapters drop their
  private tables and take `City` objects: `discover(city: City | None)`,
  `None` = every enabled city the registry lists for that source. The
  scheduler derives its city lists from the registry. `--city` resolves
  through the registry; the six ES cities keep working exactly as today.
- **UF → zone table** lives in `cities.py`; each `City.timezone` comes from it.
  Adapters stamp `record.timezone` from the city instead of a module
  constant; TicketBolt's `_wall_time()` converts in the city's zone;
  `normalize.py` is unchanged (it already honours `record.timezone`).
  BlueTicket lists `events/?state=<city.uf>`; uTicket, TicketBolt and Zig
  filter on `city.uf` instead of `STATE = "ES"`.
- **The crawl unit stays the city.** One `crawl_runs` row per source × city is
  what freshness is measured on (spec 3). "One crawl per state" for the
  state/national sources was considered and rejected: it would make
  `crawl_runs`, `mark_missing` and the refresh logic depend on the source's
  shape, for a saving the next bullet gets anyway.
- **One cycle job per source** replaces the cron job per city. The cycle
  builds one adapter and passes it, with the process-wide fetcher, to
  `run_crawl` for each of the source's cities in registry order; the feed
  cache of Zig, the per-UF listing of BlueTicket and the national list of
  TicketBolt are therefore fetched once per cycle (`discover()` of a
  state/national source caches per instance, keyed by UF where relevant).
  Trigger: every 6 h, `coalesce=True`, `max_instances=1`, so a slow cycle
  skips the next trigger instead of overlapping itself; sources run
  concurrently, each in its own bucket. The hourly refresh per source stays.
  Job ids: `cycle:<source>`, `refresh:<source>` (spec 2 adds `heartbeat`,
  spec 3 adds `health`).
- **One `Fetcher` per scheduler process.** The scheduler creates one
  `Fetcher` seeded from `REGISTRY` and hands it to every job, so a cycle and
  a refresh of the same source share the source's bucket and every run shares
  the Nominatim bucket. The CLI keeps one fetcher per invocation, as today.
- **Conditional fetch.** `discover()` may yield `Discovered(url,
  fingerprint)` next to plain `str`; the fingerprint is a sha256 of the
  listing item the source already returned (Sympla search object, uTicket
  search item incl. `updatedAt`, BlueTicket listing item, TicketBolt query
  item, Zig feed item), with volatile fields (view counts, `generated_at`,
  scores) dropped per adapter. `source_listings.listing_fingerprint` stores
  the last one. `run_crawl` skips the event page when the stored fingerprint
  equals the new one **and** the event does not start within `REFRESH_WINDOW`
  (48 h); a skipped listing counts as seen (never marked removed), bumps
  `last_seen_at`, and is counted in `CrawlSummary.skipped`. A listing with no
  stored fingerprint, or a `str` yield, is fetched as today. Task 1's
  numbers decide whether this lands before or after the capitals are enabled
  (task 8): it is built either way, because it also halves what the sources
  see from us.
- **Removal per listing, per city.** `source_listings.city` (registry slug
  the listing was discovered under) and `source_listings.removed_at`.
  `mark_missing(session, source, city, seen)` runs after every error-free,
  unlimited city crawl (`--city X` with no `--limit`; scheduler runs qualify),
  scoped to that source's listings of that city. A listing not seen sets
  `removed_at`; the event becomes `removed_from_source` (new version, as
  today) only when **all** of its listings are removed. A secondary listing
  disappearing writes no version; a removed listing seen again clears
  `removed_at` and, if the event was removed, revives it (existing rule).
  This closes ADR 0010's churn note; record it as ADR 0012.
- **Migration `0003`** (`alembic/versions/0003_phase3.py`) carries everything
  Phase 3 changes on core tables, in one PR, so the shared db migrates once:
  `crawl_runs.city` (nullable), `crawl_runs.kind` (`crawl` | `refresh` |
  `manual`), `crawl_runs.created/merged/versions/removed/review/skipped`;
  `source_listings.city`, `.removed_at`, `.listing_fingerprint`; the
  `scheduler_heartbeat` table and the SQL views of spec 2; the `sla` table
  of spec 3; the read-only `grafana_ro` role of spec 2 (password from the
  environment at migration time). Whichever of task 3 here and spec 2 task 1
  is built first carries the migration; the other adds to it before merge.
- **Self-hosted Nominatim.** Compose service `nominatim`
  (`mediagis/nominatim:5.3`, `PBF_URL` = Geofabrik Brazil, `IMPORT_STYLE=address`,
  `THREADS` from `.env`, volume `nominatim`, no host port, `restart:
  unless-stopped`, healthcheck on `/status`); `dev` and `evcrawl` get
  `NOMINATIM_URL=http://nominatim:8080` from `.env.example`. `Geocoder` keeps
  `countrycodes=br` and gains `EVCRAWL_GEOCODE_RPS` (default 1.0; raised only
  against the self-hosted instance, never the public one). The import runs
  once on the host by the owner; measured disk and hours go into
  `ops/host/README.md`. The `nominatim` volume is not backed up (reproducible
  from the PBF); `usher.service` starts the service. Falling back to the
  public instance is a URL change.
- **Rate limits unchanged.** Nothing in this spec raises a `RateLimit`.

## 4. Tasks

Each task: its own branch (`feat/national-<topic>`), gates green in the `dev`
container, PR into `main`, done-criteria checked by the reviewer. Recon tasks
produce a table in this file and an ADR when a decision is taken, not code.

1. **Recon: capitals per source** (`docs/national-recon`) — with `curl -A
   "$EVCRAWL_USER_AGENT"`, for each source and each capital: whether a
   listing exists and its size (Sympla: the `<city>-<uf>` slug and page
   count; uTicket: `place/city?term=` names and `count`; BlueTicket:
   `events/?state=<UF>` length; TicketBolt and Zig: counts per state and
   city from the one feed). Output, in this file's §2: the registry seed
   table (per-source keys), a sizing table (events, requests per cycle,
   seconds at 1 req/s, per source) and the statement of whether the sum fits
   6 h without conditional fetch. Done: tables dated and in this file; no
   crawl run from the workspace beyond `curl`.
2. **City registry + zones** — `cities.py` with the 33 cities (`enabled =
   False` for the capitals until task 8), adapters take `City` (BlueTicket
   `state=<uf>`, the three post-filters on `city.uf`, TicketBolt wall time
   in the city's zone, `record.timezone` from the city), `--city`
   resolution, `SOURCE_CITIES` derived from the registry, `tests/test_cities.py`,
   adapter tests updated with one Manaus case for TicketBolt's wall time.
   Done: every existing test passes with the six ES cities unchanged;
   `evcrawl crawl sympla --city sao-paulo --dry-run --limit 5` logs five
   events with `timezone = America/Sao_Paulo` and `--city manaus` (Zig or
   TicketBolt, whichever lists one) logs `America/Manaus`.
3. **Migration 0003 + run metadata** — models and Alembic for the columns and
   tables of §3 (coordinated with spec 2 task 1 and spec 3 task 1);
   `run_crawl`/`run_refresh` fill `crawl_runs.city`, `.kind` and the counts.
   Tested on `createdb <login>_scratch`, never on the shared db before merge.
   Done: `alembic upgrade head` and `downgrade -1` both work on scratch;
   `ops/host/status.py` still runs; `tests/test_db_models.py` covers the new
   columns.
4. **Removal per listing** — `mark_missing` per city, `removed_at`, the
   all-listings-removed rule, revival, ADR 0012. Done: integration test on
   scratch where a two-source merged event loses one listing → event stays
   active, no version; loses both → `removed_from_source` with a version;
   the listing returns → revived; `run_crawl` calls it from a `--city` run
   without `--limit`.
5. **Scheduler cycles + shared fetcher** — one cycle job per source walking
   the registry, one adapter per cycle, one process-wide `Fetcher`,
   `kind="crawl"`, `tests/test_scheduler.py` rewritten (ids
   `cycle:<source>`/`refresh:<source>`, counts, no stagger assertion),
   `scheduler.started` logs cycles and cities. Done: a two-city fixture
   cycle fetches the Zig feed once (fake fetch call count); the refresh and
   the cycle of one source use the same `Fetcher` instance; the scheduler
   command in the `runtime` image logs `scheduler.started` (host owner
   checks with `ops/host/status.py`).
6. **Conditional fetch** — `Discovered`, fingerprints in every adapter,
   the skip rule in `run_crawl`, `skipped` in `CrawlSummary`, `crawl_runs`
   and the `crawl.done` log. Done: a second fixture-fed run fetches zero
   event pages; a changed listing item fetches again; an event within 48 h
   always fetches; a skipped listing's `last_seen_at` moves and it is not
   marked removed.
7. **Self-hosted Nominatim** — compose service, `.env.example`, `Geocoder`
   rate setting, `ops/host/README.md` import recipe with the measured disk
   and hours, `usher.service` ExecStart gains `nominatim`; commit says
   "rebuild required". The owner runs the import and announces it. Done:
   `evcrawl crawl zig --city vitoria --limit 5` on the shared db resolves
   venues with `geocode.resolved` logs naming `http://nominatim:8080`;
   `ops/audit/audit.py` sees no new host port.
8. **Enable the capitals** — flip `enabled` on the 27 capitals, one
   announced scheduler cycle on the shared db (host owner starts it through
   `ops/host/release.sh`), cycle times read from `crawl_runs` into §2's
   sizing table, AGENTS.md §12 and CLAUDE.md "Current work" updated. Done:
   §5 holds.

## 5. Acceptance for the spec

- All four gates green in the `dev` container and in CI.
- One cycle per source covers all 33 cities inside 6 h, measured from
  `crawl_runs` (spec 2's freshness view once it exists).
- A listing dropped by its source is `removed_at` within two cycles and its
  event is `removed_from_source` only when no other source keeps it.
- Events in the 27 capitals have venues with coordinates for ≥ 95 % of active
  events (spec 2's geocoder view), all resolved against the self-hosted
  instance.
- No source rate-limited or blocked the project User-Agent during task 8's
  cycle; if one did, the answer was a lower rate limit or dropping the city,
  raised with the owner (AGENTS.md §10).
- ADR 0012 written; §2 tables dated and filled.

## 6. Ask the owner before

Raising any rate limit; adding cities beyond the 33; running a cycle or an
unlimited crawl from the workspace; changing the Nominatim import style or
publishing its port; any change to `events`, `venues`, `event_versions` or
`raw_snapshots` beyond migration `0003`; adding a source.
