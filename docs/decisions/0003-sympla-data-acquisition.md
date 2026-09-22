# 0003. How Sympla data is acquired

Date: 2026-09-20. Status: accepted.

## Context

Phase 1 crawls Sympla for Greater Vitória (`docs/specs/phase-1-sympla.md`).
Recon on 2026-09-20 confirmed the spec's §2: `robots.txt` allows everything;
listing pages (`/eventos/<city-slug>?page=N`) are Next.js App Router pages whose
search response travels in RSC flight chunks (`self.__next_f.push`); event
pages (`/evento/<slug>/<id>`) are Pages Router pages with `__NEXT_DATA__`.
Both carry coordinates. Prices are not in the HTML (see ADR 0004).

## Decision

- `discover(city)` walks each city's listing pages from `page=1` until a page
  yields zero events (`?page=9` for 189 events at 24 per page; safety cap 100
  pages), decodes the flight chunks, locates `"searchDataResult":` in the
  concatenated payload and `raw_decode`s the object there. It yields the
  listing's `url` field, de-duplicated per city. A listing page that fails to
  fetch or parse is logged (`sympla.listing_unavailable` /
  `sympla.listing_unparsable`) and the city is skipped; it is not fatal.
- `--city` accepts `vitoria`, `vila-velha`, `serra`, `cariacica`, `viana`,
  `guarapari` (or the `-es` slug); omitted means all six. All six slugs were
  verified to return real listings on 2026-09-20.
- `fetch(url)` parses `props.pageProps.hydrationData.eventHydration.event` from
  `__NEXT_DATA__`, then requests the tickets endpoint (ADR 0004) under the same
  rate limit. `external_id` is the numeric id at the end of the URL (checked
  against the page's `id`; a mismatch is logged).
- **Deviation from the spec:** bileto entries (`bileto.sympla.com.br/event/<id>`,
  a separate id space) get `external_id = "bileto-<id>"`, not the bare number,
  so a bileto id can never collide with a www id inside
  `source_listings (source_id, external_id)`. They are built from the listing
  object only (`raw["bileto"] = True`, `raw["price_lots_pending"] = True`),
  their page is never fetched, and `organizer.email` is dropped before the
  listing object goes into `raw`.
- Times: `*MultiFormat.ISO8601` (offset `-03:00`) converted to UTC;
  `timezone = "America/Sao_Paulo"`. Listing `start_date`/`end_date` are UTC.
- `EventRecord` gained `latitude`/`longitude` (Sympla supplies both; geocoding
  is the fallback) and `cancelled`/`closed` (from `cancelled` / `isClosed`) so
  the status mapping in the spec §3 does not have to dig into `raw`.
- Address string: `address, addressNum - addressAlt, neighborhood, city - state,
  zipCode`, empty parts skipped; this is also the geocode-cache key input.
- `description` is `strippedDetail`. `raw["event"]` keeps the whole event
  object (including the HTML `detail`) minus `eventsHost.description`, which
  is free text that may carry contact data (AGENTS.md §10).
- Category is `eventsCategory.slug`; mapping to the internal taxonomy is
  `normalize.py`'s job (task 4).
- Adapters do no HTTP themselves: every request goes through an injected
  `fetch_text(source, url)` (`evcrawl.pipeline.fetch`, implemented in task 3).
  Tests inject a fixture-backed fake.

## Consequences

- Fixtures: `tests/fixtures/http/sympla/listing-vitoria-p1.html`,
  `listing-vitoria-p9-empty.html`, `event-3537075.html`,
  `tickets-3537075.json`, sanitized with the skill's script. Remaining `@`
  characters in the HTML are CSS at-rules, RSC references and social handles;
  a test asserts that the only e-mail address present is `redacted@example.org`.
- Sympla can change either page format without notice; the flight-chunk and
  `__NEXT_DATA__` helpers fail loudly (`ValueError`) so a crawl run records the
  error instead of storing garbage.

## Addendum (2026-09-22): the hub listing template, cities from the registry

Phase 3 task 2 (`docs/specs/phase-3-national.md` §2 "Facts task 2 must build
on", §4 task 2) found a second listing template. The 11 biggest capitals
(São Paulo, Rio de Janeiro, Belo Horizonte, Brasília, Porto Alegre, Curitiba,
Salvador, Recife, Fortaleza, Goiânia, Florianópolis) answer
`/eventos/<city-slug>` with a curated **hub page** (`matrixType: city`): no
`searchDataResult`, four `collection-matrix` sliders (24 items each, their
own `total`) and one `dataSectionMoreEvents` block (`data`, `total`, `limit:
16`, `page`) that `?page=N` pages at 16 per page in `global-score-norm`
order until an empty `data` (São Paulo: `total` 3,858, page 300 empty). The
21 other cities keep the generic template of this ADR.

Decisions:

- `parse_listing_page()` returns `(template, events)`: `"city"` from
  `searchDataResult`, else `"hub"` from `dataSectionMoreEvents`, else
  `ValueError` as before. The sliders are **not read**: their items are
  city events the more-events walk lists anyway, and reading them would
  double-count against `total`. `MAX_LISTING_PAGES` is 500 (was 100; São
  Paulo needs ~241); the empty page stays the stop condition. Fixtures:
  `listing-sao-paulo-p1.html` (16 items, all in São Paulo/SP) and
  `listing-sao-paulo-p300-empty.html`, sanitized as the others.
- **The generic listing stays regional.** The owner chose (2026-09-22) to keep
  yielding every listed event whatever its `location.city`: `vitoria-es`
  lists ~190 events across ES and the six ES slugs overlap heavily, so today
  a Vila Velha event is fetched under both. Filtering to the requested city
  would drop the events of Linhares, Aracruz and other non-registry cities
  that the listings carry, and task 4 would then mark them removed. The
  overlap is left to the conditional fetch of task 6 (a second sight of an
  unchanged listing item skips the event page).
- Cities come from the registry (`src/evcrawl/cities.py`): `discover(city:
  City | None)` walks `city.keys["sympla"]` (the `<name>-<uf>` slug); `None`
  means every enabled city. `CITY_SLUGS`/`city_slugs()` are gone.
- `record.timezone` is the zone of the record's own UF
  (`cities.timezone_for_uf(state)`), not a module constant and not the crawl
  city's zone: `fetch()` also runs from the hourly refresh, which has no city,
  and a zone stamped from the crawl city would flip back on refresh. The same
  rule applies to the other four adapters (uTicket, BlueTicket, TicketBolt,
  Zig; Zig first honours the event's own zone when tzdata knows it).
