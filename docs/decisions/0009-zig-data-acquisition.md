# 0009. How Zig data is acquired

Date: 2026-09-21. Status: accepted (recon, task 7); the adapter is task 8.

## Context

Phase 2 adds Zig for Greater Vitória (`docs/specs/phase-2-sources.md` §2,
"not covered by the notebook"). Recon from scratch on 2026-09-21, every
request through `evcrawl.pipeline.fetch.Fetcher` (project User-Agent,
1 req/s, `robots.txt` check). The public ticketing site is
`https://zig.tickets` (the company's marketing site is `zig.fun`; `zigpay.com.br`
is the payments product and has no event listing).

- `robots.txt` (`https://zig.tickets/robots.txt`, 200): `User-agent: *`,
  disallows account, checkout, legal and `/static/` paths; **`/eventos/` and
  `/` are allowed**. It also carries a `Content-Signal: search=yes,
  ai-input=yes, ai-train=no` line: indexing and answering from the content
  is allowed, training is not — this project does neither; it stores public
  event data. `Sitemap: https://zig.tickets/sitemap.xml`. The S3 host of the
  events feed (below) answers `403 AccessDenied` (XML) for `/robots.txt`,
  which the fetch layer treats as allow-all.
- **The site is a Next.js Pages-Router app, server-rendered.** Every page
  ships `<script id="__NEXT_DATA__">` with its props; no client-side API
  call is needed for the data this project wants.
- **The listing is a public static JSON feed.** The home page's
  `pageProps.events` is byte-for-byte the content of
  `https://zigtickets-static-zig-tickets.s3.us-east-1.amazonaws.com/domains/zig.tickets/new-events.json`
  (the site's own config names it `resources.newEvents`): a plain GET, no
  auth, `application/json`, 525 kB, regenerated about every minute
  (`generated_at`). Keys: `states` (17 UFs with events), `events` (**the
  national list of upcoming events, 273 on 2026-09-21, 42 in ES**),
  `categories`, `top_highlights`, `middle_highlights`,
  `middle_highlights_by_state`. Greater Vitória: Vitória 15, Vila Velha 6,
  Guarapari 6, Serra 4, Cariacica 2, Viana 0 (33 events).
- Feed item: `id` (numeric), `name`, `slug`, `start_date`/`end_date`
  (ISO 8601 **with the offset**, `2026-11-07T15:00:00.000-03:00`),
  `timezone` (`America/Sao_Paulo`; three events say `Brazil/DeNoronha`, one
  `Brazil/East`), `is_online`, `is_closed`, `banner`/`thumb`/`vertical_banner`,
  `event_location` (`name`, `formatted_address`, `neighborhood`, `city`,
  `state` (UF), `zip_code`, `lat`/`lng` — **null on 272 of 273**),
  `event_categories[]` and `event_subcategories[]` (`id`, `name`, `image`),
  `organization` (`id`, `name`, `slug`, … **`phone`, `email`** on 199 of
  273 — organizer contact data, AGENTS.md §10), `hide_event_end_date`.
  Six items have a `start_date` in the past with an `end_date` in the
  future (multi-day or long-running events); none is `is_closed` or online.
  Ids and slugs are unique.
- **The event page is server-rendered.** `GET https://zig.tickets/eventos/<slug>`
  (225-250 kB, half of it i18n `messages`) has in `pageProps`:
  - `event`: `id`, `name`, `slug`, `description` (**HTML**, often inline-styled),
    `start_date`/`end_date` (with offset), `timezone`, `currency` (`BRL`),
    `is_online`, `is_closed`, `is_public`, `age_range`, `has_sessions`,
    `event_location` (as the feed plus `complement`, `country`),
    `producer` (`name`, `slug`, `type`, `thumbnail` — no contact fields),
    `event_sectors[].event_sector_lots[]` (lot prices without names),
    `json_ld` (schema.org `Event`: `startDate`, `endDate`, `eventStatus`,
    `location` with a `PostalAddress`, `image`, text `description`), and
    tracking/checkout configuration (`fb_pixel_id`, `g_*`, `gateway`,
    `expiration_minutes`, …). **No category** — only the feed has it.
  - `tickets`: `availables[]` — the price lots on sale: `name`
    (`Pista [Meia] 3º Lote`), `sector_name`, `value` and `fee` in
    **reais** (decimals), `public`, `available`, `lot_available`,
    `is_passport`, `minimum`/`maximum`; `unavailables[]` (sold out or not
    yet on sale), `cart_discount`.
  - The same JSON-LD is also in a `<script type="application/ld+json">`.
- Other pages: `/eventos` (listing shell, no data in `__NEXT_DATA__`),
  `/categorias/<slug>`, `/localidades/<zip>` (968 zip-code pages in
  `sitemap-locations.xml`, not city pages), `sitemap-events.xml` (5,229
  URLs, past events included). None beats the feed for discovery.

## Decision

- **Discovery from the feed, record from the event page.**
  1. `discover(city)` — one `GET` of `new-events.json`, keeps items with
     `event_location.state == "ES"` and `city` in the requested Greater
     Vitória cities (spec §4 state-level rule; all six when `--city` is
     omitted), skips `is_closed` and items whose `end_date` (or
     `start_date` when there is none) is in the past, and yields
     `https://zig.tickets/eventos/<slug>`.
  2. `fetch(url)` — `GET` the event page, read `__NEXT_DATA__`; the feed
     item for the slug (loaded once per adapter instance, lazily, so the
     hourly refresh works from `source_url` alone) supplies the category.
     Record: `external_id = event.id`, `title = name`, `starts_at`/`ends_at`
     from the offset-carrying ISO strings (aware; `timezone` from the event
     when `zoneinfo` knows it, else `America/Sao_Paulo`), `venue_name =
     event_location.name`, `address = formatted_address`, `city`, `state`,
     `latitude`/`longitude` when present (else the geocoder),
     `organizer = producer.name`, `category` = slug of the first
     `event_categories[].name`, `description` = the HTML turned into text,
     `image_url = json_ld.image` (else `banner`), `closed = is_closed`,
     `cancelled` when `json_ld.eventStatus` ends in `EventCancelled`.
- Price lots: `tickets.availables[]` with `public == 1`, `amount = value +
  fee` (what the buyer pays, as Sympla's fee-included price, uTicket's
  `price + taxPrice`, BlueTicket's `valor + valor_conveniencia`), named
  `name` prefixed with `sector_name` when the name does not already carry
  it. `value == 0` → a free lot. No available lot → no lots and
  `raw["price_lots_pending"] = True` (ADR 0004).
- Categories: `SOURCE_CATEGORIES["zig"]` maps the ten category names
  (slugified as BlueTicket's `category_slug`): `shows-e-festivais` →
  `music`, `festas-e-baladas`/`festas-e-bares`/`sazonais-e-feriados` →
  `party`, `teatro-museus-e-cultura` → `art-and-culture`,
  `parques-passeios-e-tours` → `tour`, `esportes` → `sports`,
  `workshops-e-cursos` → `course`, `palestras-e-congressos` → `conference`,
  `eventos-privados` → `uncategorized`. Subcategories are kept in `raw`.
- **Contact data never leaves the adapter**: `raw["listing"]` is the feed
  item with `organization` reduced to `id`, `name`, `slug`;
  `raw["event"]` is the page's `event` without `description`, `json_ld`,
  `sponsors` and the tracking/checkout keys (`fb_*`, `g_*`, `gateway`,
  `default_acquirer`, `seats_integration_*`); `raw["tickets"]` is
  `availables` as received (no personal data in it).
- Rate limit: the default `RateLimit(1.0, 5)`, one bucket for `zig.tickets`
  and the S3 host. A full Greater Vitória crawl is 1 feed GET (525 kB) +
  33 page GETs (~8 MB).
- No headless browser is needed (owner decision 4 holds).

## Consequences

- Fixtures (`tests/fixtures/http/zig/`): `new-events.json` (the feed
  trimmed to the ES items plus a few of other states, `organization`
  contact fields blanked), `event-vintage-vila-velha.html` and
  `event-samba-em-vix-pagode-da-garagem.html` (the pages with
  `pageProps.messages` and `<script src>` tags dropped, producer names
  kept — they are companies, not persons — and any e-mail or phone
  redacted). Sanitized with the skill's script; 0 e-mails.
- The feed is national and 525 kB; the six per-city jobs each fetch it
  once per run (6 × 525 kB per 6 h), as BlueTicket's jobs do with their
  state listing.
- If the feed URL changes, the home page's `__NEXT_DATA__`
  (`pageProps.events`, same shape, 963 kB) is the fallback; the adapter
  logs `zig.feed_unavailable` and yields nothing until the constant is
  updated.
- `Brazil/DeNoronha`/`Brazil/East` are legacy aliases that the image's
  `zoneinfo` does **not** know (`ZoneInfoNotFoundError`, checked
  2026-09-21). The adapter keeps an event's `timezone` only when
  `zoneinfo` accepts it and falls back to `America/Sao_Paulo`; the offset
  in the ISO string wins for the instant anyway, the `timezone` column only
  records the zone.
