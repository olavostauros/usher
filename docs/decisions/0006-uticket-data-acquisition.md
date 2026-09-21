# 0006. How uTicket data is acquired

Date: 2026-09-21. Status: accepted — parsing done (task 1); the `Referer`
question below was decided by the owner the same day (see "Addendum") and the
adapter is registered and crawling (task 2).

## Context

Phase 2 adds uTicket for Greater Vitória (`docs/specs/phase-2-sources.md` §2).
Recon on 2026-09-21 with `curl -A "$EVCRAWL_USER_AGENT"` re-verified the
spec's facts and found what the notebook had not recorded:

- `https://uticket.com.br/robots.txt` allows `/` for `*` and disallows only
  account and back-office paths (`/me/`, `/painel/`, `/organizador/`,
  `/ticket/`, ...). `/event/<id>` is allowed. `https://api.uticket.com.br/`
  serves no `robots.txt` (403, see below), so the fetch layer's check treats
  the API host as allow-all.
- The event page (`https://uticket.com.br/event/<id>`) is a client-rendered
  React app: 2.7 kB of HTML, generic `<title>`, no embedded data. Its bundle
  reveals the API the page calls, all on `https://api.uticket.com.br`:
  - `GET /eventsearch?limit=N&skip=M&city=<name>&q=<term>` →
    `{"count": N, "items": [...]}`. Items: `id` (opaque, e.g.
    `01M4IIJK3JGTH9`), `userId`, `userImgUrl`, `name`, `startDate`, `endDate`
    (UTC, `Z`), `sessions` (always the zero date; useless), `minPrice`,
    `maxPrice` (reais, no tax), `place` (`"<venue or street> - <City>-<UF>"`),
    `updatedAt`. **No coordinates, address, category, description or image.**
  - `GET /eventinfo/<id>` → the public event: `name`, `description` (HTML,
    double-encoded: a JSON string inside the JSON string), `place` (`name`,
    `address` in the same `- <City>-<UF>` format, `position.lat/lng`),
    `schedules` and `sessions` (UTC), `status` (integer, meaning not found in
    the bundle), `hidden`, `UserName` (organizer account name),
    `userEmail` (empty on the sample, still a contact field).
  - `GET /tickettype?eventId=<id>` → `ticketTypes[]` with `fullname`, `price`
    and `taxPrice` in **cents**, `sellStart`/`sellEnd`, `restricted`,
    `remainingQuantity`. `minPrice` on the search item equals the cheapest
    `price` without `taxPrice`.
  - `GET /place/city?term=<text>` → list of city names; the value the site
    puts in `city=`. All six Greater Vitória names resolve (`Vitória`,
    `Vila Velha`, `Serra`, `Cariacica`, `Viana`, `Guarapari`). The API has
    **no state filter**: `Viana` would also match Viana-MA.
- `city=Vila Velha&q=` returns 6 events, all in Vila Velha-ES; the spec's
  `q="vila velha"` returned 5 and included one in Campos dos Goytacazes-RJ
  (free-text match), so `city=` is the right parameter.
- Pagination: `skip += limit` until `skip + len(items) >= count`; past the
  end the API answers `{"count": N, "items": []}`. `limit=50` is accepted.
- Event images: `https://img.uticket.com.br/event/<id>/s` (small) and `/l`
  (large) return `image/jpeg`; without a size suffix the host answers 405.

### Blocked: the API requires a `Referer`

Every request to `api.uticket.com.br` **without** `Referer: https://uticket.com.br/`
is answered `403` by Cloudflare's WAF ("Sorry, you have been blocked"), with
or without `Accept: application/json`, with or without an `Origin` header.
The same request with that one header, still carrying the project
User-Agent, is answered `200`. So the block is not on our User-Agent; it is a
rule that gates the API to requests that look like they come from the site's
own pages.

AGENTS.md §10 forbids disguises (User-Agent rotation, fingerprint spoofing,
proxies) and says a block is a conversation with the owner. A fixed `Referer`
is not on that list and the bot stays honestly identified, but it is a header
sent to satisfy a rule meant to keep non-browser clients out. That is the
owner's call, not the adapter's. The recon and fixture requests of this task
(about fifteen) sent the header, as the spec's notebook did; the adapter does
not, and it is not registered, so nothing crawls uTicket until the owner
decides (`~/pr-bodies/OWNER-QUESTIONS.md`).

## Decision

- `discover(city)` walks `eventsearch?limit=50&skip=M&city=<name>&q=` per
  city until the page is empty or `skip + len(items) >= count` (safety cap
  100 pages), yields `https://uticket.com.br/event/<id>` de-duplicated per
  city, and skips items whose `place` ends in another state's `-<UF>`
  (`uticket.other_state`). An unavailable or unparsable search page is logged
  (`uticket.search_unavailable` / `uticket.search_unparsable`) and the city
  is skipped, not fatal.
- `--city` accepts `vitoria`, `vila-velha`, `serra`, `cariacica`, `viana`,
  `guarapari` (short key, `-es` slug or the API's own name); omitted means all
  six.
- `fetch(url)` takes the canonical URL (so the hourly refresh, which re-fetches
  `source_listings.source_url`, needs no cached search item), requests
  `eventinfo/<id>` and then `tickettype?eventId=<id>` under the same rate
  limit. The search item is not used to build the record.
- **Deviation from spec §4:** no `A partir de` lot from `minPrice`. Real lots
  come from `tickettype`; when that request fails the record has no lots and
  `raw["price_lots_pending"] = True`, exactly as Sympla does (ADR 0004).
- Prices: `amount = (price + taxPrice) / 100`, what the buyer pays, matching
  Sympla's `salePriceMonetary` (ADR 0004). Lots with `restricted = true`
  (private link) or `sellEnd` in the past are skipped, like Sympla's hidden
  and closed lots. A `price` of 0 becomes a `PriceLot(amount=0)`.
- Times: the API gives UTC (`Z`); `starts_at` is the earliest `schedules`
  start and `ends_at` the latest end (fallback `sessions`);
  `timezone = "America/Sao_Paulo"`.
- Venue: `place.name`, `place.address` verbatim (it is also the geocode-cache
  key input), `city`/`state` parsed from the `- <City>-<UF>` suffix,
  coordinates from `place.position`.
- `description` is the HTML turned into plain text (block elements become
  line breaks, `&nbsp;` collapsed). `raw["event"]` keeps the whole `eventinfo`
  object, HTML included, minus `userEmail` (AGENTS.md §10). `UserName` is
  kept as the organizer name: it is what the public page shows.
- Category: the API has none; `category = None` → `uncategorized` in
  `normalize.py`. `status`/`hidden` are kept in `raw` only; `cancelled` and
  `closed` stay `False` until the enum is understood (a cancelled event has
  not been observed yet).
- `image_url = https://img.uticket.com.br/event/<id>/l`, built, not fetched.
- Adapters do no HTTP themselves; every request goes through the injected
  `fetch_text`. Tests inject a fixture-backed fake.

## Consequences

- Fixtures: `tests/fixtures/http/uticket/search-vila-velha-p1.json`
  (`limit=50&skip=0&city=Vila Velha`), `search-vila-velha-empty.json`
  (`skip=50`), `event-01M4IIJK3JGTH9.json`, `tickets-01M4IIJK3JGTH9.json`,
  sanitized with the skill's script. Two edits by hand: `UserName` (a natural
  person's name) became `Produtora Exemplo`, and the empty `userEmail` became
  `redacted@example.org` so a test can prove the adapter strips it.
- If the owner allows the `Referer`, task 2 needs `Fetcher` to send per-source
  extra headers (a small change in `src/evcrawl/pipeline/fetch.py`, declared
  on the adapter next to `rate_limit`) plus the registry and scheduler wiring.
  If not, the adapter stays as offline parsing code or is removed; the
  alternative data path (`sitemap-eventos.xml`, 1.2 MB of event URLs) leads
  to the same API, so it does not help.
- Sessions with several `schedules` are stored as one event spanning the
  whole range; per-session rows are a schema question for later.

## Addendum (2026-09-21): the `Referer` is allowed

Owner decision, 2026-09-21 (`~/pr-bodies/OWNER-DECISIONS.md` §1, option A of
the question above): the adapter **may send the fixed header
`Referer: https://uticket.com.br/`** on every `api.uticket.com.br` request.
The User-Agent stays the project's descriptive one and the rate limit is
unchanged. Asking uTicket for formal API access is a courtesy step the owner
keeps for later.

How it is implemented (task 2):

- `SourceAdapter.extra_headers` (`src/evcrawl/sources/base.py`), declared next
  to `rate_limit`, is a mapping of fixed headers the HTTP layer adds to **that
  source's requests only**. `UTicketAdapter.extra_headers = {"Referer":
  "https://uticket.com.br/"}`; every other adapter keeps the empty default.
- `Fetcher(headers={source: {...}})` (`src/evcrawl/pipeline/fetch.py`) merges
  them into each request of that source, including the `robots.txt` fetch of
  the API host, and **drops any `User-Agent` key** so the header mechanism can
  never become a disguise (AGENTS.md §10). `run_crawl` builds the mapping
  from `REGISTRY`, next to the rate limits.
- Unit tests: `tests/test_pipeline_fetch.py`
  (`test_extra_headers_apply_to_their_source_only_and_keep_the_user_agent`)
  and `tests/test_sources_registry.py` (no registered adapter declares a
  User-Agent; uTicket declares exactly the `Referer`).
- With the header, `https://api.uticket.com.br/robots.txt` is fetched by the
  fetch layer like any host's; what it answers is recorded in the task 2 PR.

Status of this ADR: accepted.
