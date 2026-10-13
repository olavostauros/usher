# 0023. How Bilheteria Digital data is acquired

Date: 2026-10-13. Status: accepted (`docs/SPECIFICATION.md` §8.3, task 2).

## Context

Wave 1 task 2 is Bilheteria Digital (`www.bilheteriadigital.com`), a
mainstream platform strong in shows, parties and theatre in the Southeast.
Recon on 2026-10-13 from the host, with the project's User-Agent at 1 req/s.

The §8.3 gate:

1. **robots.txt.** `www.bilheteriadigital.com/robots.txt` is
   `User-agent: *` / `Disallow: /voucher/` only. `checkout2.bilheteriadigital.com`
   (below) serves the same file.
2. **Terms of use** (`bd.bilheteriadigital.com/termos-uso`, updated
   2026-05-06): nothing on robots, crawling, scraping or automated access.
3. **Plain client.** Every request below answered 200 to the project's
   User-Agent with no cookie and no extra header. No bot wall, no waiting
   room.
4. **Listing and event read.** Both are server-rendered HTML (below).
5. **Overlap.** Not measured from the host (no database there); left for
   the `dev` container before merge (the PR body says so).

The site is server-rendered PHP. There is no public JSON API, no sitemap
(`/sitemap.xml` is 404), no JSON-LD and no embedded data object for the
listing, so the adapter reads HTML with selectolax.

- **Listing.** `GET /<UF>/as/1` returns the HTML fragment the site's state
  picker loads (`eventosPorEstado` in `/assets/busca.js`): every event of
  the state on one page; `/<UF>/as/2` is 404. The UF must be upper case: an
  unknown path such as `/es/as/1` answers 200 with São Paulo's listing, so
  cards are also filtered by their own UF. Cards are
  `li.box-li-evento > a[href="/<slug>"]` with `p.titulo-evento-thumb`,
  `p.data-evento-thumb` (`31 de Julho`, no year, empty for a running
  exhibition), `div.cidade-box-evento` (`Cariacica - ES`),
  `div.local-box-evento` (venue) and an image on S3. A mustache template
  card (`{{cidade}} - {{estado}}`) sits in a script and is ignored. Counts
  on 2026-10-13: RJ 42, SP 21, AC 7, MG 3, ES 2, RR 1, AP 1.
- **Event page.** `GET /<slug>`. Hidden inputs carry `eventoId`,
  `nomeEvento`, `local` (venue), `slug` and `dataInicio`, the start in UTC
  (`2027-07-31T14:30:00.000Z`, shown as "Sábado às 11:30").
  `h3.textinfo` starts with `<city> - <UF>`. `input#coordenadas` holds
  `lat,lng`. `og:image` is the poster. The description is
  `div.informacoes-adicionais-evento`; organizers often write an
  `Endereço: ...` line there, the only street address on the page.
  `div.topo-data-group` shows the date, or a range (`30 Out A 01 Nov`).
- **Prices.** One `div.linhaIngressoTr[data-ingresso-id]` per ticket, with
  the name in `p.ingresso-titulo` and `data-ingresso-valor` /
  `data-ingresso-taxa` (price and service fee, in reais, dot decimal).
- **Calendar events** (exhibitions with daily sessions, e.g. `black-mirror`)
  redirect to `checkout2.bilheteriadigital.com/<slug>` (same page template,
  4.6 MB) and add `window.BD_CALENDARIO = {"sessoes": {"YYYY-MM-DD":
  {"HH:MM": ...}}}`; every ticket row repeats per session, with the session
  in its name (`13/10- INGRESSO INTEIRA | SESSÃO 14h00`).
- **Zones.** The site shows every time in Brasília time: a Rio Branco (AC)
  event shown "Sexta às 19:00" has `dataInicio` 22:00 UTC, i.e. 19:00 at
  UTC-3. Buyers read 19:00 as local.
- No category: `estiloId` is the page layout (33 is the agenda layout), not
  a genre. The producer's company name appears only in analytics code.

## Decision

- `discover()` fetches each UF's `/<UF>/as/1` once per adapter instance and
  keeps the cards whose UF is the city's and whose city folds to the
  registry name (the display name, as for the JSON sources).
- The external id is the **slug**: the card links to it, `fetch()` requests
  it, and `discover()` knows it without the page (ADR 0013). `eventoId` is
  kept in `raw`. The fingerprint is the card: slug, title, date, city, UF,
  venue and image URL (its `?<timestamp>` changes when the poster does).
- `fetch()` requests the canonical `/<slug>` URL (redirects followed), so a
  404 is a gone page (ADR 0022).
- `starts_at` is `dataInicio` read as the Brasília wall time and placed in
  the UF's zone (no change for UTC-3 states; Acre and Amazonas move by one
  or two hours). The timezone is the UF's.
- `ends_at`: a calendar event ends at its last session; a date range ends
  on its last day at the start's wall time (the header has no end time, as
  Ingresse's sessions have none); otherwise none.
- Price lots are price + fee, the buyer's price (as Sympla and Zig), one per
  ticket row, the session stripped from calendar ticket names and repeats
  dropped. A page with no ticket rows (sold out, sales closed) leaves the
  lots empty, which `normalize` marks pending.
- No category (`uncategorized`) and no organizer: the company name is not
  shown on the page and can be a person's name (AGENTS.md §10).
- Address: the description's `Endereço:` line when there is one; the
  page's own coordinates come first for geocoding anyway.
- No extra headers. Rate limit 1 req/s, 5 retries (the default). No
  headless browser.

## Consequences

- A full cycle costs one listing request per UF in the registry (27), plus
  one page per new or changed card, or per event starting within 48 h.
- Many events in the registry's cities sit in towns around them (São
  Bernardo do Campo, Campos dos Goytacazes) that the registry does not list;
  they are not collected. On 2026-10-13: São Paulo 6 of SP's 21, Rio de
  Janeiro 33 of RJ's 42, Cariacica 2 of ES's 2.
- Calendar pages are large (4.6 MB for one exhibition); the fingerprint
  keeps them to one fetch per change or per 48 h window.
- Everything lands in `uncategorized` until the site exposes a genre.
- If the site starts showing times in each event's own zone, `starts_at`
  for UTC-4/UTC-5 states shifts; the fixture test for Rio Branco pins the
  current reading.
