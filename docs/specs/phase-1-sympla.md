# Phase 1 build spec: Sympla adapter and pipeline for Greater Vitória

Status: **built** (tasks 1-10 done 2026-09-21; see "Outcome" below). Written
2026-09-20 from read-only recon of sympla.com.br. Execute the tasks in order,
one branch and PR per task. AGENTS.md rules apply throughout; the
`add-source-adapter` skill covers the adapter mechanics.

## Outcome (2026-09-21)

Tasks 1-10 are done. The Sympla adapter (ADR 0003: city listing pages as RSC
chunks, the server-rendered event page, the `event-bff` tickets endpoint for
lots, with lots first deferred by ADR 0004) crawls Greater Vitória through
the pipeline (`discover → fetch → normalize → geocode → dedupe → upsert`),
the full schema of migration `0002` is in place, geocoding runs against
Nominatim with a cache, and the dedupe scaffold links listings to events.
The release smoke passed on 2026-09-21: `migrate` then `crawl sympla --city
vitoria --limit 5` in the `runtime` image, 5/5 upserted. The first
pre-release image is the `v0.1.0-dev.1` tag on GHCR (ADR 0005); it runs as
the `evcrawl` service of the Compose project on the owner's computer, the
only deployment target.

## 1. Goal

`evcrawl crawl sympla --city vitoria --limit 10` fetches ten upcoming Sympla
events in Greater Vitória (ES), normalizes them, stores them with venues and
coordinates in the shared PostGIS database, and records a `crawl_runs` row.
Running it again changes nothing unless the source changed. `evcrawl scheduler`
registers the standing jobs. The `runtime` image runs the same code.

Out of scope for Phase 1: uTicket/TicketBolt/Zig, cross-source merging (only the
matching scaffold), ticket price lots for paid events (see §4), national coverage.

## 2. Recon facts (2026-09-20)

Verify these before relying on them; sites change. Record deviations in ADR 0003.

**robots.txt**: `User-agent: *` / `Allow: /`. Crawling is permitted; keep the
descriptive User-Agent from settings and 1 request/second.

**Listing pages** `https://www.sympla.com.br/eventos/<city-slug>?page=N`
- Next.js App Router page served from `discovery-next.svc.sympla.com.br`.
- No `__NEXT_DATA__`, no JSON-LD. The data is in React Server Components flight
  chunks: `self.__next_f.push([1,"<escaped JSON>"])`. Collect every chunk's second
  element, JSON-unescape each string (`json.loads('"' + chunk + '"')`), concatenate.
- The concatenated payload contains the search response: `"total":189`,
  `"limit":24`, `"page":N`, `"sort":"location-score"`,
  `"location":"<lat>,<lon>"`, `"state":"ES"` and 24 event objects per page.
- Event object keys: `id`, `name`, `url`, `start_date`, `end_date` (UTC ISO 8601
  with `+00:00`), `location{name,address,address_num,address_alt,neighborhood,
  city,state,zip_code,country,lat,lon}`, `organizer{id,name,email,url,image}`,
  `images{original,xs,lg}`, `type`, `event_type`, `duration_type`, `company`,
  `global_score`, `start_date_formats`, `end_date_formats`.
- Pagination ends when a page yields zero events (`?page=9` for 189/24).
- Some entries point to `https://bileto.sympla.com.br/event/<id>` (a different
  Sympla product, its own id space; the listing `id` is not the URL id). The rest
  are `https://www.sympla.com.br/evento/<slug>/<id>`.
- `organizer.email` is personal/contact data: never store it, strip it from
  fixtures.

**Event pages** `https://www.sympla.com.br/evento/<slug>/<id>`
- Next.js Pages Router: `<script id="__NEXT_DATA__" type="application/json">`.
- Path: `props.pageProps.hydrationData.eventHydration.event`.
- Useful keys: `id`, `name`, `slug`, `newUrl`, `startDate`/`endDate` (local wall
  time `"YYYY-MM-DD HH:MM:SS"`), `startDateMultiFormat.ISO8601` /
  `endDateMultiFormat.ISO8601` (with `-03:00` offset), `eventsAddress{name,address,
  addressNum,addressAlt,neighborhood,city,state,zipCode,country,geolocation{
  latitude,longitude}}`, `eventsCategory{id,name,slug,vertical}`,
  `eventsHost{name,url,organizerId,organizerPageSlug,image,description}`,
  `cancelled`, `isClosed`, `published`, `visible`, `privateEvent`, `onlineInfo`
  (null for in-person), `images{logoUrl,logoLarge,logoSmall,logoCover}`,
  `strippedDetail` (plain-text description), `detail` (HTML),
  `paymentEventType` (`"paid"` | `"free"`).
- Ticket prices are not in the HTML; the page loads them client-side. See §4.
- `eventsHost.description` is free text and may contain contact data; do not
  store it.

**Coordinates** come from Sympla in both pages, so geocoding is a fallback for
records without `geolocation`, not the main path.

## 3. Design decisions (already taken; record them in ADR 0003)

- `discover(city)` yields the canonical event URL string. `fetch(url)` parses the
  event page. The numeric id at the end of the URL is `EventRecord.external_id`.
- bileto URLs: Phase 1 records them from listing data only (title, dates, venue
  from the listing object), sets `raw["bileto"] = True`, and does not fetch the
  bileto page. They are still `source="sympla"`.
- City slugs for Greater Vitória: `vitoria-es`, `vila-velha-es`, `serra-es`,
  `cariacica-es`, `viana-es`, `guarapari-es`. `--city vitoria` maps to
  `vitoria-es`; `--city` omitted means all six. Verify each slug returns a real
  listing; a 404 or zero events on page 1 is logged, not fatal.
- Times: parse `*MultiFormat.ISO8601` (offset-aware), store UTC, keep
  `timezone = "America/Sao_Paulo"`. Listing `start_date` is already UTC.
- Status mapping: `cancelled` → `cancelled`; `isClosed` or `endDate` in the past
  → `ended`; not seen on a re-crawl of its city → `removed_from_source`; content
  changed → `updated` with a new `event_versions` row; otherwise `scheduled`.
- Categories: map `eventsCategory.slug` to the internal taxonomy in
  `normalize.py`; unknown → `uncategorized` plus a warning log with the slug.
- Free events: one `PriceLot(name="Gratuito", amount=0)`. Paid events: empty
  `price_lots` and `raw["price_lots_pending"] = True` until §4 is solved.
- Description: store `strippedDetail`; keep `detail` only inside the compressed
  raw snapshot.

## 4. Deferred: ticket price lots

Prices load through a client-side request that the recon did not capture. Do
not add Playwright for this. Task 1 includes a 30-minute recon in a browser's
network tab (or by reading the page's JS chunk that renders tickets) to find the
endpoint; if it is a plain JSON GET keyed by event id, add it to `fetch()` as a
second request under the same rate limit. Otherwise write ADR 0004 with what
was found and leave `price_lots_pending` for Phase 2.

## 5. Tasks

Each task: its own branch (`feat/sympla-<topic>`), gates green, PR into `main`.
Done-criteria are what the reviewer checks.

1. **Fixtures and adapter parsing** — `tests/fixtures/http/sympla/listing-vitoria-p1.html`,
   `listing-vitoria-p9-empty.html`, `event-3537075.html`, sanitized with the
   skill's script; `SymplaAdapter.discover()` and `fetch()` in
   `src/evcrawl/sources/sympla.py` parsing those pages; a small
   `_flight_payload(html)` helper and `_next_data(html)` helper with their own
   tests; `tests/test_sources_sympla.py`. Update `tests/test_sources_registry.py`
   (fetch is now implemented). Done: parsing tests pass offline; no `@` in
   fixtures except `redacted@example.org`.
2. **Schema** — `src/evcrawl/db/models.py` gains `Source`, `Event`,
   `EventVersion`, `Venue` (`location geography(Point,4326)` + GiST index),
   `Organizer`, `Category`, `PriceLot` (`numeric(12,2)` + `currency`),
   `SourceListing` (unique on `source_id, external_id`; keeps `source_url`),
   `RawSnapshot` (bytea gzip + sha256), `GeocodeCache` (unique normalized
   address). All timestamps `timestamptz`. Migration
   `alembic/versions/0002_core_tables.py`. Test the migration on
   `createdb <login>_scratch` (`DATABASE_URL=...` override), never on the shared
   db until merged. Done: `alembic upgrade head` and `downgrade base` both work
   on the scratch db; models import under `mypy --strict`.
3. **HTTP layer** — `src/evcrawl/pipeline/fetch.py`: one `httpx.AsyncClient`
   per run with the User-Agent from settings, a per-source token bucket honoring
   `RateLimit`, exponential backoff with jitter on 429/5xx up to `max_retries`,
   a `robots.txt` check cached per host, structured logs (`source`, `url`,
   `status`, `attempt`). Adapters call `fetch_text(source, url)`. Tests use
   `httpx.MockTransport`. Done: retry and rate-limit behavior covered by tests.
4. **Normalization** — `src/evcrawl/pipeline/normalize.py`: `EventRecord` →
   `NormalizedEvent` (title trimmed/collapsed, `title_normalized` lowercase
   unaccented, UTC datetimes, category mapping, price rule, address string for
   the geocode cache key). Done: table-driven tests including accents, free vs
   paid, unknown category.
5. **Geocoding** — `src/evcrawl/pipeline/geocode.py`: `geocode_cache` lookup by
   normalized address; call Nominatim (settings URL, 1 req/s, User-Agent) only
   when the record has no coordinates; write through to the cache. Tests never
   hit the network (mock transport). Done: an event with source coordinates
   makes zero geocoder calls.
6. **Upsert** — `src/evcrawl/pipeline/upsert.py`: within one transaction per
   event: venue/organizer/category get-or-create, `source_listings` upsert,
   `events` insert or update with status transition, `event_versions` row only
   when the normalized content hash changed, `raw_snapshots` gzip of the source
   payload. Never delete. Done: integration test against the scratch db proves
   idempotence (second run: 0 new versions).
7. **Dedupe scaffold** — `src/evcrawl/pipeline/dedupe.py`: `match_key()` and
   `find_candidates(session, normalized)` using `title_normalized`, start ±2 h,
   `ST_DWithin(location, point, 300)`; sets `events.dedupe_review = true` on an
   ambiguous match; no merging yet. Done: unit tests for the key, one SQL test
   on the scratch db.
8. **CLI wiring** — `src/evcrawl/cli.py` `crawl`: discover → fetch → normalize →
   geocode → upsert with `--city`, `--dry-run` (parse and log, no writes),
   `--limit`; writes one `crawl_runs` row (start, end, discovered, fetched,
   upserted, errors, `error_detail`); exit code 1 if every fetch failed.
   Done: `uv run evcrawl crawl sympla --city vitoria --limit 10` on the shared db
   (announce it) produces 10 events with venues and coordinates and one
   `crawl_runs` row; running it twice adds no versions.
9. **Scheduler** — `src/evcrawl/scheduler.py` `build_scheduler`: one job per
   city every 6 h (staggered), plus an hourly job re-fetching events starting
   within 48 h. Not run from the workspace. Done: `build_scheduler()` returns
   the expected jobs (unit test), `evcrawl scheduler` starts and logs
   `scheduler.started` with `jobs=7` in the runtime image (host owner checks).
10. **Release smoke** (host owner) — `docker build --target runtime`,
    `docker compose run --rm evcrawl migrate`, `docker compose run --rm evcrawl
    crawl sympla --city vitoria --limit 5`. Update AGENTS.md §12 when done.

## 6. Acceptance for the phase

- All four gates green in the `dev` container and in CI.
- Task 8's done-criterion holds on the shared db.
- No organizer e-mail, no attendee data, in the database, fixtures or logs.
- ADRs written: `docs/decisions/0003-sympla-data-acquisition.md`,
  `docs/decisions/0004-price-lots-deferred.md` (or the endpoint, if found).

## 7. Ask the owner before

Adding an external service, changing core table schemas beyond task 2, changing
rate limits, adding Playwright or any headless browser (including for prices),
changing pinned versions or the base image, resetting the shared database,
crawling anything outside Greater Vitória.
