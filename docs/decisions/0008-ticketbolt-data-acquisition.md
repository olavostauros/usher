# 0008. How TicketBolt data is acquired

Date: 2026-09-21. Status: accepted (recon, task 5); the adapter is task 6.

## Context

Phase 2 adds TicketBolt for Greater Vitória (`docs/specs/phase-2-sources.md`
§2). The spec's notebook drove the site with Playwright and never looked for
the request behind the state picker; the owner ruled out a headless browser
in either image (decision 4, 2026-09-21). Recon the same day with the project
User-Agent only — every request went through `evcrawl.pipeline.fetch.Fetcher`
(User-Agent from settings, 1 req/s, `robots.txt` check) or, for the two POST
probes, `httpx` with the same User-Agent — found a plain JSON endpoint:

- `robots.txt`: `https://www.ticketbolt.com/robots.txt` is a 404 (the
  Next.js app's own 404 page, 543 kB of HTML); `https://little-pika-375.convex.cloud/robots.txt`
  is an empty 404. No path is disallowed; the fetch layer treats both hosts
  as allow-all. There is no `sitemap.xml` either (same 404 page).
- **The site is a Next.js app on Vercel with a Convex backend.** The home
  page (`/`, 559 kB, 28 RSC flight chunks, one of them 448 kB of i18n
  strings) carries **no event data**: the listing is rendered on the client
  from a Convex query. The 33 JS chunks the page loads name the deployment
  (`https://little-pika-375.convex.cloud`, `ConvexReactClient`) and the
  queries the pages call: `api.events.getPublicEventsList`,
  `api.events.search`, `api.eventDates.getEventDates`.
- **The state picker is client-side.** The header calls
  `getPublicEventsList({country: "BRA", now: Date.now()})` once, keeps the
  events whose `eventEndDateTime || eventDate >= now`, and derives the state
  list from each event's `state` (`extractUniqueStates`). "Selecione o
  estado" → "Espírito Santo" filters that array in memory; no request per
  state exists.
- **The listing query answers a plain POST without any token or key.** Convex
  exposes every query at `POST https://<deployment>.convex.cloud/api/query`
  with body `{"path": "events:getPublicEventsList", "args": {...}, "format":
  "json"}` (also `POST /api/run/events/getPublicEventsList`). The site
  itself only fires the query after Convex auth is ready — anonymous
  visitors mint a token from the Next.js route `/api/auth/convex-public-token`,
  which requires an `x-api-key` embedded in the bundle — but the query is
  **not** gated: without `Authorization`, without the key, with the project
  User-Agent, it returns `{"status": "success", "value": {"events": [...],
  "pagination": {...}}}` (200, 183 kB). Nothing from the site's auth path is
  replayed by this project; the embedded `x-api-key` is deliberately not
  used (AGENTS.md §10: a frontend credential is not ours to present).
- `GET /api/query?path=…&args=…` is refused (`BadQueryArgs`, the server does
  not deserialize `args` from a query string, whatever the encoding);
  `GET /api/run/…` is 405. The endpoint is POST-only, which the fetch layer
  did not support before task 6.
- Listing item (`value.events[]`): `_id` (opaque Convex id, e.g.
  `mx70htxeapzmtajj4ymmbqe0k98bdhds`), `name`, `category` (display name:
  `Festas e Shows`, `Teatros e Espetáculos`, `Stand Up Comedy`,
  `Esportes`), `city`, `state` (UF), `location` (free text: `"<venue>,
  <street>, <number> - <City>/<UF>"`), `description` (often empty),
  `eventDate`, `eventEndDateTime` (epoch **milliseconds**, see "Times"),
  `is_cancelled`, `status` (`Publicado` for everything public), `imageUrl`,
  `heroImageUrl`, `images` (storage ids), `visualizacoes`,
  `featuredOnHomepageHero`. **No slug, no coordinates, no price lots, no
  organizer.** On 2026-09-21: 100 events nationally, 21 in ES (Vitória 15,
  Serra 3, Cariacica 1, Vila Velha 1, Linhares 1).
- Pagination: without `page` the query returns the whole list
  (`pagination: {currentPage: 1, hasNextPage: false, totalEvents: 100,
  totalPages: 1}`), which is what the site does. With `page: N` it pages by
  16 (`totalPages: 7` for the same 100). `limit` is rejected (`Server
  Error`). Whether 100 is the total or a server cap could not be told
  apart on the day (both counts were exactly 100); the adapter follows
  `hasNextPage` with `page` so a cap would show up as a second page.
- **The event page is server-rendered.** Cards link to
  `/event/<slug>` where `slug = generateEventSlug(name)`: lowercase, NFD
  with combining marks removed, `[^\w\s-]` removed (ASCII `\w`), whitespace
  runs → `-`, `-` runs collapsed, trimmed (`"CARNAVAL DE VITÓRIA  2027
  29/01"` → `carnaval-de-vitoria-2027-2901`). `GET
  https://www.ticketbolt.com/event/<slug>` (600 kB, still mostly the i18n
  chunk) carries, in one RSC flight row, `initialEventDetail` with:
  - `event`: `_id`, `slug`, `name`, `city`, `state`, `country`, `zipCode`,
    `location`, `category` + `categoryKey` (`festas_shows`) + `subject`
    (`music`), `eventDate`, `eventEndDateTime`, `salesPeriod`,
    `is_cancelled`, `status`, `visibilidade` (`public`), `type`
    (`presencial`), `producerName` (the organizer as shown on the page; on
    the sample a natural person's name), `description` — a reference
    (`"$25"`) to a text row of the same stream (`25:T7fe,<markdown>`),
    empty string when there is none; plus payment/pixel/fee config that the
    adapter drops (`paymentMethods`, `paymentAuth`, `pixelTrackings`,
    `webFeeConfigId`, `userId`).
  - `datesListing.eventDates[]`: `dateISO` (local midnight as UTC),
    `startTime`/`endTime` (`"14:00"`/`"23:00"` wall clock), `isAvailable`.
  - `datesListing.lotes[]`: the price lots — `ticketSectorName` (`ARENA
    SOMOS ROCK`), `ticketTitle` (`Inteira`, `Meia`, `Cortesia`, …), `name`
    (`4º Lote`), `price` (**reais**, integer or decimal), `isActive`,
    `courtesyTicket`, `remainingQuantity`, `salesPlatform`.
  - `imageUrls.main` / `.banner` (absolute `convex.cloud/api/storage/…`
    URLs), `availability`, `stageMap`.
- **Times.** `eventDate` is the local wall time serialized as if it were
  UTC: `1793541600000` = `2026-11-01T14:00Z` while the page shows
  `startTime: "14:00"`; `1790370000000` = `2026-09-25T21:00Z` with
  `startTime: "21:00"`; `dateISO` `2026-11-01T03:00:00Z` is local midnight.
  So the epoch is read as a naive America/Sao_Paulo wall time, not as UTC.
- No coordinates anywhere; `location` (free text, with the venue name
  first) plus `zipCode` go to the geocoder (the query chain of the
  2026-09-21 geocode fix strips the postal code and falls back to street
  and venue name).
- Descriptions can carry an organizer's phone number in the text ("Contato
  EXCLUSIVO para aquisição de lounges: …"). It is public event content and
  is kept in `description` like any other source's; fixtures redact it.

## Decision

- **No headless browser (owner decision 4).** The data path is the Convex
  query for discovery and the server-rendered event page for the record:
  1. `discover(city)` — one `POST
     https://little-pika-375.convex.cloud/api/query` with
     `{"path": "events:getPublicEventsList", "args": {"country": "BRA",
     "now": <epoch ms>}, "format": "json"}` (plus `"page": N+1` while
     `pagination.hasNextPage`), keeps events with `state == "ES"` and
     `city` in the requested Greater Vitória cities (spec §4: state-level
     source, filtered by city after discovery; all six when `--city` is
     omitted), skips `is_cancelled` and non-`Publicado` items, and yields
     `https://www.ticketbolt.com/event/<generateEventSlug(name)>`.
  2. `fetch(url)` — `GET` the event page, decode the `self.__next_f.push`
     chunks into one RSC stream, find the row holding `initialEventDetail`,
     resolve the `description` reference to its `T` text row, and build the
     `EventRecord`: `external_id = event._id`, `title = name`,
     `venue_name` = the part of `location` before the first comma, `address`
     = the rest plus `zipCode`, `city`/`state` from the event, no
     coordinates, `organizer = producerName`, `category = categoryKey`,
     `description` = the markdown text, `image_url = imageUrls.main` (or
     `banner`), `cancelled = is_cancelled`, `closed` when `status` is
     `Cancelado`/`Finalizado`.
- **Fetch layer:** `Fetcher.fetch_text(source, url, json=...)` sends a
  `POST` with a JSON body when `json` is given (same bucket, retries, robots
  check and headers as a GET); the `FetchText` protocol gains the optional
  keyword. Nothing else in the layer changes; GET stays the default.
- Price lots: the `lotes` with `isActive`, not `courtesyTicket` and a
  numeric `price`, named `"<ticketSectorName> - <ticketTitle> - <name>"`,
  `amount = price` in reais. `Cortesia` lots are not public prices and are
  skipped even when active. When no lot is active the record has no lots and
  `raw["price_lots_pending"] = True` (ADR 0004). `price` on the event (the
  cheapest lot) is not a lot.
- Times: `eventDate`/`eventEndDateTime` → naive wall time
  (`datetime.fromtimestamp(ms / 1000, UTC).replace(tzinfo=None)`) in
  `America/Sao_Paulo`, converted to UTC by `normalize.to_utc`.
- Categories: the bundle's table maps the 14 display names to keys
  (`festas_shows`, `festas_juninas`, `teatros_espetaculos`,
  `stand_up_comedy`, `passeios_tours`, `esportes`, `congressos_palestras`,
  `infantil`, `cursos_workshops`, `gastronomia`, `pride`, `eventos_online`,
  `religiao_espiritualidade`, `tecnologia`; anything else is slugified the
  same way). `categoryKey` maps through `SOURCE_CATEGORIES["ticketbolt"]`
  to the internal taxonomy (`festas_shows` → `party`, `teatros_espetaculos`
  and `stand_up_comedy` → `theatre-and-dance`, `esportes` → `sports`, …);
  unknown keys → `uncategorized` and logged. `subject` (`music`) is kept in
  `raw` only.
- `raw["event"]` is the `event` object minus `paymentMethods`,
  `paymentAuth`, `pixelTrackings`, `webFeeConfigId`, `userId`;
  `raw["lots"]` the lots as received; `raw["event_dates"]`; `raw["listing"]`
  is not kept (the record comes from the page, so the hourly refresh works
  from `source_url` alone).
- Rate limit: the default `RateLimit(1.0, 5)`; one bucket for both hosts.
  A full ES crawl is 1 POST + 21 page GETs (~13 MB, mostly the i18n chunk);
  requesting the RSC payload alone (`RSC: 1` header, ~50 kB) would cut that
  by ten but is an extra header with no owner decision yet, so not now.

## Consequences

- Fixtures (`tests/fixtures/http/ticketbolt/`): `query-getPublicEventsList.json`
  (the page-less response, trimmed to the 21 ES events plus 3 of other
  states, `pagination` kept), `query-getPublicEventsList-p2.json` (a real
  `page: 2` response, trimmed), `event-somos-rock.html` and
  `event-lagun-bin.html` (the event pages with the 448 kB i18n flight chunk
  and the `<script src>` tags removed, `producerName` replaced by `Produtora
  Exemplo`, the phone number in the description redacted). Sanitized with
  the skill's script; 0 e-mails.
- The slug is derived from the name; a renamed event becomes a new listing
  (`source_url` changes), exactly as on the site.
- `page 1 = 16 items` only when `page` is sent: the adapter sends no `page`
  on the first request and only follows `hasNextPage`.
- If Convex or the site starts requiring the anonymous token, the adapter
  stops with `ticketbolt.listing_unavailable` and the owner decides; the
  embedded `x-api-key` path is not an option for this project.

## Addendum (2026-09-21): `RSC: 1` on the event pages, headers per host

Owner decision 6, 2026-09-21 (`~/pr-bodies/OWNER-DECISIONS.md`, option A of
run 2's question 5): the adapter **may send `RSC: 1`** to
`www.ticketbolt.com`, the header Next.js itself sends on client navigation,
so the event page answers the flight stream (`text/x-component`) instead of
the rendered HTML page. The Convex host gets nothing extra; the User-Agent
and the rate limit are unchanged.

How it is implemented (run 3, task R):

- `SourceAdapter.extra_headers` is now **keyed by host**:
  `{"<hostname>": {header: value}}`. `Fetcher` applies a header set only to
  that source's requests whose `urlsplit(url).hostname` matches the key
  (case-insensitively), including that host's `robots.txt`; any other host
  of the same source, and any other source, gets none of them. A
  `User-Agent` key is still dropped by construction (AGENTS.md §10).
  `UTicketAdapter.extra_headers = {"api.uticket.com.br": {"Referer": ...}}`
  (ADR 0006) and `TicketBoltAdapter.extra_headers = {"www.ticketbolt.com":
  {"RSC": "1"}}`; every other adapter keeps `{}`. Tests:
  `tests/test_pipeline_fetch.py` (`…_apply_to_their_source_and_host_only_…`)
  and `tests/test_sources_registry.py`.
- `fetch()` reads **whichever arrived**: `is_html(body)` (the body starts
  with `<`) selects the page path, which decodes the `self.__next_f.push`
  chunks as before; otherwise the body is the raw flight stream and is
  parsed as is (`flight_stream`). The record built from either is the same
  (`test_record_from_rsc_payload_equals_record_from_page`), so a server that
  ignores or stops honouring the header costs nothing. The adapter logs
  `ticketbolt.event_payload` with `kind` (`rsc`/`html`) and `bytes` per
  event.
- Fixture `tests/fixtures/http/ticketbolt/event-somos-rock.rsc.txt`: the
  `RSC: 1` response of the same page as `event-somos-rock.html`, recorded
  2026-09-21 18:17 UTC through `Fetcher`, sanitized the same way (the i18n
  `messages` row dropped, `producerName` → `Produtora Exemplo`, the phone
  number redacted with a same-length string so the `T<len>,` row stays
  valid), 65 kB. The two fixtures were recorded hours apart, so only the
  live counters (`visualizacoes`, `remainingQuantity`) differ between them.

**Measured size difference (2026-09-21, `Fetcher`, both event pages):**

| Request | somos-rock | lagun-bin |
|---|---|---|
| `GET /event/<slug>` (HTML page) | 614,768 B | 584,667 B |
| same with `RSC: 1` | 521,178 B | 497,793 B |

`RSC: 1` alone saves **~15 %**, not the ~90 % run 2 estimated: the flight
stream still carries the root layout's i18n `messages` row (456 kB of the
521 kB; the rest is the 25 kB `initialEventDetail` row, the 2 kB
description text row and ~40 module/CSS rows). The ~50 kB figure of run 2
is what the site's own router receives on a client navigation because it
also sends `Next-Router-State-Tree`, which tells the server which layouts
the client already holds; the server then skips them. Measured once for
the record (the same page with `RSC: 1` plus a state tree naming another
event page): **32,025 B**, i.e. −95 %. That second header is not sent by
this project: it encodes a client navigation state, which is outside the
decision taken, and is raised as a run 3 owner question. A full ES crawl
(21 pages) is therefore ~11 MB instead of ~13 MB with this change, and
would be ~0.7 MB with the state tree.

Status of this ADR: accepted.
