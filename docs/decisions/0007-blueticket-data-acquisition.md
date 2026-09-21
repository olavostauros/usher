# 0007. How BlueTicket data is acquired

Date: 2026-09-21. Status: accepted.

## Context

Phase 2 adds BlueTicket for Greater Vitória (`docs/specs/phase-2-sources.md`
§2, in scope since 2026-09-21). Recon the same day with
`curl -A "$EVCRAWL_USER_AGENT"` re-verified the spec's facts and corrected two:

- `robots.txt`: `https://www.blueticket.com.br/robots.txt` returns the app
  shell (HTML, 200), i.e. there is none, so no path is disallowed;
  `https://api2.blueticket.com.br/robots.txt` is a JSON 404;
  `https://soulapi.blueticket.com.br/robots.txt` is `User-agent: * /
  Disallow:` (allow all). The fetch layer treats all three as allow-all.
- **The event page is not server-rendered** for the project User-Agent.
  `https://www.blueticket.com.br/evento/<id>` returns the same 3.4 kB Nuxt
  shell as every other path (`window.__NUXT__` holds only config); the
  `<title>` the notebook saw was rendered by its browser. The HTML carries no
  event data, so it is never fetched. The bundles reveal the API instead.
- **The search endpoint answers a plain GET** with the project User-Agent,
  no `Referer`, `Accept` or cookie needed. `origin` and `pdv` change nothing
  in the response: `origin` is an analytics tag (`capa`, `busca-rapida`),
  `pdv` the store id (100 = the public site, from `store/configs`).
- `GET https://api2.blueticket.com.br/events/?<filters>` takes `search`,
  `terms`, `state`, `city`, `region`, `category`, `minDate`, `maxDate`,
  `maxSaleDate`, `organizer`, `venue`, `aggregated`; **there is no
  pagination** (the frontend has none either). `city=` is ignored server-side
  (`city=Vila Velha` returned the same 42 events as `state=ES`), so
  **`state=ES` is the listing**: 42 events on 2026-09-21 (Vitória 32,
  Cariacica 6, Vila Velha 4), 39 kB. `search=Vila Velha` (the spec's call)
  returns 4, `search=Vitória` 32: it matches city and title text.
- Listing item: `id` (numeric), `name`, `slug`, `venue`, `cityName`,
  `cityState`, `category` (display name or `null`), `date` (local wall time,
  `YYYY-MM-DD HH:MM:SS`), `dateTs` (its UTC epoch: `2026-09-26 21:00:00`
  → `1790467200` = `2026-09-27T00:00Z`, so wall times are
  America/Sao_Paulo), `eventTime` (show start, doors are `date`),
  `cancellationDate` (`"0"` when not cancelled), `indefiniteDate`,
  `mainEventId`, `venueType` (`local`/`online`/`hibrido`), `onSale`, `url`
  (cover image). **No coordinates, address, description or organizer.**
- `GET /events/<id>/detail` → `info` (`name`, `description` HTML, `category`
  `{id, name}`, `ageRestriction`), `dates.dateTs`, `sale` (`isCancelled`,
  `saleWindow`), `venue` (`name`, `address` free text, `city`, `state`,
  `type`, `latitude`/`longitude` — **0/0 when the organizer did not fill
  them in**, as on the sample), `organizer` (`name`, social links,
  `description`), `media[]` (`url`), `configs`, `integrations` (pixel
  tokens). `GET /events/<id>` is a 404.
- Prices are not in the detail; the page loads them from the store API:
  `GET https://soulapi.blueticket.com.br/api/pdv/v3/products/sectors/<id>?event=<id>`
  with `PDV: 100` / `POS: 100` headers (410 "Evento não disponível" without
  them). **The same values work as query parameters** (`&pdv=100&pos=100`),
  so no header support is needed in the fetch layer. Response:
  `sectors[].products[]` with `nome`, `nome_tipo` (`Inteira`, `Meia-entrada`,
  ...), `tipo_produto` (`I` = ticket), `valor`, `valor_conveniencia` (reais,
  floats), `lote`, `esgotado`, `encerrado`.
- `GET /events/categories`: Shows, Teatros, Infantil, Baladas, Cursos e
  Workshops, Congressos, Esportivos, Exposições, Festivais; listings also
  carry `Carnaval` and `Stand Up`.
- The v2 listing the home page uses
  (`api-cdn.blueticket.com.br/api/v2/events/list?cidade=&uf=ES&agregados=1`)
  is the "next 8 events" of the state, not a listing; not used.

## Decision

- **Deviation from spec §2/§5:** discovery is state-wide, like §4's
  state-level sources. `discover(city)` makes **one** request,
  `events/?state=ES`, and yields `https://www.blueticket.com.br/evento/<id>`
  for items whose `cityState` is `ES` and `cityName` is one of the requested
  Greater Vitória cities (accent-insensitive), de-duplicated by id. An
  unavailable or unparsable listing is logged (`blueticket.listing_unavailable`
  / `blueticket.listing_unparsable`) and yields nothing. The fixture is
  therefore `search-es.json`, not `search-vila-velha.json`.
- `--city` accepts `vitoria`, `vila-velha`, `serra`, `cariacica`, `viana`,
  `guarapari` (short key, `-es` slug or the API's `cityName`); omitted means
  all six. Only Vitória, Vila Velha and Cariacica had events on 2026-09-21.
- `fetch(url)` takes the canonical URL (with or without the `/<slug>` suffix;
  `source_url` is stored without it), requests `events/<id>/detail` and then
  `products/sectors/<id>?event=<id>&pdv=100&pos=100` under the same rate
  limit. When the products call fails the record has no lots and
  `raw["price_lots_pending"] = True` (ADR 0004). The listing item is not
  used to build the record, so the hourly refresh works from `source_url`.
- Prices: `amount = valor + valor_conveniencia` (what the buyer pays online,
  matching Sympla's fee-included price, ADR 0004), exact `Decimal` from the
  float's string. Only `tipo_produto == "I"` (tickets); `encerrado` lots are
  skipped, `esgotado` (sold out) ones kept. Lot name is
  `<sector or product> - <nome_tipo>` (`Ingresso - Inteira`) so half-price
  and full-price lots stay distinct.
- Times: `starts_at = dates.dateTs` (doors open, the datetime the site shows
  as the event's); `eventTime` (show start) stays in `raw`. `ends_at` is
  `None` (`showEndTime` was 0 on every listing). `timezone =
  "America/Sao_Paulo"`.
- Venue: `venue.name`, `venue.address` with whitespace collapsed (it is also
  the geocode-cache key input), `venue.city`/`state`; coordinates only when
  both are non-zero, otherwise `None` and the geocoder resolves the address.
- `cancelled = sale.isCancelled`; `venue.type == "online"` sets
  `raw["online"]`. `closed` stays `False` (the pipeline marks ended events by
  date).
- Category: `info.category.name` slugified (`shows`, `stand-up`,
  `cursos-e-workshops`) and mapped in `normalize.py` (`SOURCE_CATEGORIES["blueticket"]`):
  Shows/Festivais → music, Baladas/Carnaval → party, Teatros/Stand Up →
  theatre-and-dance, Infantil → kids, Cursos e Workshops → course,
  Congressos → conference, Esportivos → sports, Exposições →
  art-and-culture. `null` → `uncategorized` (7 of 42 listings).
- `description` is the HTML turned into plain text. `raw["event"]` keeps
  the whole detail minus `integrations` (tracking tokens) and
  `organizer.description` (free text that may carry contact data, AGENTS.md
  §10). `organizer.name` is the organizer.
- `image_url` is the first `media[].url`.
- Adapters do no HTTP themselves; every request goes through the injected
  `fetch_text`. Tests inject a fixture-backed fake.

## Consequences

- Fixtures: `tests/fixtures/http/blueticket/search-es.json`
  (`events/?state=ES`), `event-41709.json` (`events/41709/detail`),
  `products-41709.json`, sanitized with the skill's script (no e-mail
  present). One edit by hand: the organizer's WhatsApp number inside the
  description became `5500000000000`.
- Three hosts are crawled (`api2`, `soulapi`, and `www` only as the canonical
  URL); `Fetcher` checks `robots.txt` per host and rate-limits per source, so
  the 1 req/s budget is shared by all BlueTicket requests.
- With no pagination, a state listing much larger than today's 42 would
  arrive in one response; the adapter logs the count
  (`blueticket.listing`) so growth is visible.
- Events whose organizer left coordinates at 0/0 depend on the geocoder and
  on the free-text `address` (the sample: `Praça Assis chateaubriand,60 Ibes
  - Vila Velha - cep:29.108.630`); geocoding quality on such strings is a
  task-4 observation.
