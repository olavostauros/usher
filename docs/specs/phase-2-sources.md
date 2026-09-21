# Phase 2 build spec: uTicket, BlueTicket, TicketBolt, Zig adapters and live dedupe

Status: recon only, written 2026-09-21; BlueTicket added to scope by the owner
the same day. The facts in §2 come from a teammate's
exploratory notebook run against the live sites on 2026-09-21; the notebook
itself is not in the repository (see §3 for why). Nothing here has been
re-verified with the project User-Agent or against each site's `robots.txt`;
the first task of every adapter does that. AGENTS.md rules apply throughout;
the `add-source-adapter` skill covers the adapter mechanics.

## 1. Goal

`evcrawl crawl uticket --city vila-velha --limit 10` (and the same for every
other Phase 2 adapter) fetches upcoming events in Greater Vitória (ES) through
the existing pipeline: discover → fetch → normalize → geocode → dedupe → upsert,
with a `crawl_runs` row. `evcrawl crawl all` covers every registered source.
Cross-source dedupe merges listings of the same event into one canonical
`events` row instead of only flagging `dedupe_review`. `evcrawl scheduler`
carries the new sources' standing jobs. The `runtime` image runs the same code.

Out of scope for Phase 2: national coverage, monitoring dashboards, freshness
SLAs (Phase 3).

## 2. Recon facts (2026-09-21)

Verify these before relying on them; sites change. Record deviations in an ADR
per source (`docs/decisions/000N-<source>-data-acquisition.md`), as ADR 0003
did for Sympla.

### uTicket (roadmap)

- **Public JSON API, no auth**:
  `GET https://api.uticket.com.br/eventsearch?limit=12&skip=0&city=&q=<term>`.
  The notebook sent `Accept: application/json` and `Referer: https://uticket.com.br/`;
  whether either is required is unverified.
- Response: `{"items": [...], "count": N}`. Item keys seen: `id` (opaque
  string, e.g. `01M4IIJK3JGTH9`), `name`, `place`, `startDate`, `endDate`,
  `minPrice`, `maxPrice`, `updatedAt`. The full item shape was not recorded;
  look for coordinates, address, category, description, image and organizer
  before deciding whether a detail endpoint is needed.
- Pagination: `skip += limit` until `len(items) >= count`.
- Canonical event URL: `https://uticket.com.br/event/<id>`. The page is
  client-rendered (every event page's `<title>` was the generic site title),
  so the API is the data source; fetching the HTML is pointless.
- `q="vila velha"` with `city=""` returned 5 events. Whether `city=` accepts a
  real value (and what values) is unverified; if it does, prefer it over `q`.
- `robots.txt` not checked yet.

### TicketBolt (roadmap)

- **JavaScript SPA** at `https://www.ticketbolt.com/`. The notebook drove it
  with Playwright: click the button "Selecione o estado", then "Espírito
  Santo", wait for the text "Explore eventos em Espírito Santo". Cards are
  `div.cursor-pointer.flex-col` with `h2` (name), `p` (category), first `span`
  (location), second `span` (date); clicking a card navigates to
  `https://www.ticketbolt.com/event/<slug>`. 21 events for ES.
- The listing is **state-level**, not city-level; `--city` must filter after
  discovery (see §4).
- **The notebook did not look for the JSON request behind the state picker.**
  Capturing it (browser network tab filtered to XHR/fetch, or a throwaway
  Playwright script logging `page.on("response")` URLs) is task 3. The
  browser is a recon tool only; a headless browser inside the adapter is the
  last resort of AGENTS.md §6 step 3 and needs the owner's approval (§11).
- `robots.txt` not checked yet.

### Zig (roadmap)

Not covered by the notebook. Recon from scratch per the skill's §1 (task 7).

### BlueTicket (roadmap since 2026-09-21)

- **JSON API** captured behind the site's search:
  `GET https://api2.blueticket.com.br/events/?search=<term>&origin=capa&pdv=100`
  returns a JSON list of event objects with at least `id` (numeric), `name`,
  `venue`, `cityName`, `cityState`, `category`, `displayDatetime`.
- Canonical event URL: `https://www.blueticket.com.br/evento/<id>`. Pages are
  server-rendered (the `<title>` matched the event name), so an event page
  fetch may add description, image and coordinates.
- Unverified: whether the API answers a plain GET with the project User-Agent
  (the notebook captured it through a browser context with a spoofed Firefox
  User-Agent), what `origin` and `pdv` mean, pagination, `robots.txt`.
- `search=Vila Velha` returned 4 events.
- Owner decision 2026-09-21: in scope. It goes right after uTicket (tasks 3-4)
  because it is the cheapest remaining source: API first, server-rendered pages.

### FeverUp (not on the roadmap; rejected)

Static HTML at `https://feverup.com/en/vitoria`, event links `/m/<id>/<lang>`.
Of 13 hits, 8 were gift cards and the rest Candlelight concerts. Not worth an
adapter; do not add.

### Sympla (confirmation of ADR 0003)

The notebook independently reproduces the `"searchDataResult":` extraction
from the RSC flight chunks and shows an alternative discovery path:
`https://www.sympla.com.br/eventos?s=&page=N` with a cookie
`s_user_location={"city":..., "coordinates":{"lat":..., "lon":...}, "sk":...,
"slug":...}` (Vila Velha: `sk=3176`, `slug=vila-velha-es`) returned 33 events.
Fallback only; the per-city slug path in `src/evcrawl/sources/sympla.py`
already works.

## 3. What the notebook did that this project must not do

Each item is already a rule; the notebook is why they are restated here.

- Spoofed browser User-Agents on every request (`Mozilla/5.0`, a Firefox
  string). AGENTS.md §6 step 5: the descriptive User-Agent from settings.
- No `robots.txt` check. §6 step 4: every request goes through
  `evcrawl.pipeline.fetch`, which checks it.
- Stated aim of "not looking like a bot", rotating VPNs and spoofing hardware
  in a container. §10: never evade blocking; a block is a conversation with
  the owner, not a disguise.
- Stored the full HTML and text of every event page, including whatever
  organizer contact data the page carries. §10 (LGPD): collect only public
  event data, keep personal data minimal and out of raw snapshots.
- One schemaless SQLite table per source keyed by URL. §7: the SQLAlchemy
  models are the single source of truth; adapters return `EventRecord`.

The notebook and its `event_finder.db` stay out of the repository for these
reasons.

## 4. Design decisions (take now; record per-source details in the ADRs)

- Every adapter subclasses `SourceAdapter` (`src/evcrawl/sources/base.py`),
  makes every request through the injected `fetch_text` from
  `src/evcrawl/pipeline/fetch.py` (rate limit, retries, `robots.txt`, project
  User-Agent), registers in `REGISTRY` (`src/evcrawl/sources/__init__.py`)
  and adds its cities to `SOURCE_CITIES` in `src/evcrawl/scheduler.py`.
- API-first sources (uTicket, BlueTicket): `discover()` yields the
  API's event id (or the canonical URL); `fetch()` builds the `EventRecord`
  from the API item, calling a detail endpoint or the event page only when
  the search item lacks coordinates, address or description. `raw` keeps the
  API item minus any contact field. `fetch_text` returns `str`; the adapter
  `json.loads` it.
- State-level sources (TicketBolt): `discover(city)` walks the state listing
  once and yields only events whose location matches the requested city
  (`CITY_SLUGS`-style table per adapter, all of Greater Vitória when `city`
  is omitted). `run_crawl(source, city=...)` keeps its signature.
- Fixtures: JSON responses under `tests/fixtures/http/<source>/`
  (`search-<city>-p1.json`, `search-<city>-empty.json`, `event-<id>.json` or
  `.html`), sanitized with the skill's script; the same
  `redacted@example.org` rule as Phase 1.
- Times: parse what the API gives (check whether it is UTC or `-03:00`),
  store UTC, `timezone = "America/Sao_Paulo"`.
- Prices: `minPrice`/`maxPrice` are not price lots. Record real lots when a
  tickets endpoint exists; otherwise one `PriceLot` from `minPrice` named
  `A partir de` with `raw["price_lots_pending"] = True`, and free events as
  `Gratuito`/`0`.
- Dedupe live: `find_candidates` (`src/evcrawl/pipeline/dedupe.py`) keeps
  its matching keys (AGENTS.md §7); on exactly one confident candidate the
  upsert links the new `source_listings` row to the existing `events` row and
  writes an `event_versions` row; on several candidates it keeps the listing
  separate with `dedupe_review = true`, as today. Which fields the canonical
  row takes from which source (first seen wins, except empty fields filled
  from any source) goes into an ADR.

## 5. Tasks

Each task: its own branch (`feat/<source>-<topic>` or `feat/dedupe-<topic>`),
gates green in the `dev` container, PR into `main`, done-criteria checked by
the reviewer. Recon tasks produce an ADR, not code.

1. **uTicket recon and parsing** — re-verify §2 with `curl -A
   "$EVCRAWL_USER_AGENT"`: `robots.txt`, the search endpoint without the
   `Referer`, `city=` values, the full item shape, a detail endpoint if one
   exists. ADR `0006-uticket-data-acquisition.md`. Fixtures
   `tests/fixtures/http/uticket/search-vila-velha-p1.json`,
   `search-vila-velha-empty.json`; `UTicketAdapter.discover()` and `fetch()`
   parsing them; `tests/test_sources_uticket.py`. Done: parsing tests pass
   offline; no `@` in fixtures except `redacted@example.org`.
2. **uTicket wiring** — registry, `SOURCE_CITIES`, scheduler test updated,
   `evcrawl crawl uticket --city vila-velha --limit 5` on the shared db
   (announce it) produces events with venues and coordinates and one
   `crawl_runs` row; running it twice adds no versions.
3. **BlueTicket recon and parsing** — re-verify §2 with `curl -A
   "$EVCRAWL_USER_AGENT"`: `robots.txt`, whether the search endpoint answers
   a plain GET, what `origin` and `pdv` do, pagination, the full item shape,
   and what the server-rendered event page adds (description, image,
   coordinates). ADR `0007-blueticket-data-acquisition.md`. Fixtures
   `tests/fixtures/http/blueticket/search-vila-velha.json` plus one
   sanitized event page if `fetch()` needs it; `BlueTicketAdapter.discover()`
   and `fetch()`; `tests/test_sources_blueticket.py`. Done: as task 1.
4. **BlueTicket wiring** — as task 2, for `crawl blueticket`.
5. **TicketBolt recon** — find the JSON behind the state picker and the card
   list; note pagination, id, timezone, prices. ADR
   `0008-ticketbolt-data-acquisition.md`. If there is no usable endpoint,
   the ADR says so and the owner decides on Playwright before any adapter
   code (§7).
6. **TicketBolt adapter** — shape depends on task 5; same done-criteria as
   tasks 1-2, plus the city filter of §4 covered by a test.
7. **Zig recon** — skill §1 from scratch. ADR `0009-zig-data-acquisition.md`.
8. **Zig adapter** — as tasks 1-2.
9. **Dedupe live** — `src/evcrawl/pipeline/dedupe.py` and
   `src/evcrawl/pipeline/upsert.py` implement §4's merge rule; ADR for the
   field-precedence rule. Done: integration test on the scratch db where the
   same event from two sources (fixtures) yields one `events` row, two
   `source_listings` rows and no `dedupe_review`; an ambiguous pair still
   flags.
10. **Phase close** — update AGENTS.md §12, and §3's layout comment listing
   the adapter files.

## 6. Acceptance for the phase

- All four gates green in the `dev` container and in CI.
- Tasks 2, 4, 6 and 8's done-criteria hold on the shared db.
- `evcrawl scheduler` in the `runtime` image logs `scheduler.started` with
  every source's city jobs plus one refresh job per source (host owner
  checks).
- No organizer e-mail, no attendee data, in the database, fixtures or logs.
- One ADR per source and one for the dedupe merge rule.

## 7. Ask the owner before

Adding a headless browser to either image target (task 5's outcome), adding
any source outside the roadmap, changing rate limits, changing core table
schemas beyond what task 9 needs, resetting the shared database,
crawling anything outside Greater Vitória.
