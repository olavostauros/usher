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

**Capitals and zones** (the zone table; the full registry seed with
`ibge_code` and per-source keys is the task 1 recon below):

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

### Task 1 recon: capitals per source (2026-09-22)

Measured on 2026-09-22 with `curl -A "$EVCRAWL_USER_AGENT"` only: 151
requests, all `200`, no source blocked or rate-limited the project
User-Agent; listings only, no event page, no crawl run (branch
`docs/national-recon`). Raw responses stayed in the agent's scratchpad (Zig's
feed carries organizer contact fields). Note: "33 cities" above counts
Vitória twice; the registry has **32** distinct cities.

**Registry seed** (the `City` rows of `cities.py`, task 2; Greater Vitória
first, then capitals by UF). A source cell in backticks is the exact string
the source returned this run; a cell in parentheses means the source has a
listing there but showed zero events today, so the string is unobserved and
task 2 matches it accent- and case-folded (`_key()`, as BlueTicket, TicketBolt
and Zig already do); `—` would mean no listing at all (none this run).

| slug | name | UF | ibge_code | timezone | sympla | uticket | blueticket | ticketbolt | zig |
|---|---|---|---|---|---|---|---|---|---|
| `vitoria` | Vitória | ES | 3205309 | `America/Sao_Paulo` | `vitoria-es` | `Vitória` | `Vitória` | `Vitória` | `Vitória` |
| `vila-velha` | Vila Velha | ES | 3205200 | `America/Sao_Paulo` | `vila-velha-es` | `Vila Velha` | `Vila Velha` | `Vila Velha` | `Vila Velha` |
| `serra` | Serra | ES | 3205002 | `America/Sao_Paulo` | `serra-es` | `Serra` | (Serra) | `Serra` | `Serra` |
| `cariacica` | Cariacica | ES | 3201308 | `America/Sao_Paulo` | `cariacica-es` | `Cariacica` | `Cariacica` | `Cariacica` | `Cariacica` |
| `viana` | Viana | ES | 3205101 | `America/Sao_Paulo` | `viana-es` | `Viana` | (Viana) | (Viana) | (Viana) |
| `guarapari` | Guarapari | ES | 3202405 | `America/Sao_Paulo` | `guarapari-es` | `Guarapari` | (Guarapari) | (Guarapari) | `Guarapari` |
| `rio-branco` | Rio Branco | AC | 1200401 | `America/Rio_Branco` | `rio-branco-ac` | `Rio Branco` | (Rio Branco) | (Rio Branco) | (Rio Branco) |
| `maceio` | Maceió | AL | 2704302 | `America/Maceio` | `maceio-al` | `Maceió` | `Maceió` | `Maceió` | `Maceió` |
| `manaus` | Manaus | AM | 1302603 | `America/Manaus` | `manaus-am` | `Manaus` | `Manaus` | `Manaus` | (Manaus) |
| `macapa` | Macapá | AP | 1600303 | `America/Belem` | `macapa-ap` | `Macapá` | (Macapá) | (Macapá) | `Macapá` |
| `salvador` | Salvador | BA | 2927408 | `America/Bahia` | `salvador-ba` | `Salvador` | (Salvador) | (Salvador) | `Salvador` |
| `fortaleza` | Fortaleza | CE | 2304400 | `America/Fortaleza` | `fortaleza-ce` | `Fortaleza` | `Fortaleza` | `Fortaleza` | `Fortaleza` |
| `brasilia` | Brasília | DF | 5300108 | `America/Sao_Paulo` | `brasilia-df` | `Brasília` | `Brasília` | `Brasília` | `Brasília` |
| `goiania` | Goiânia | GO | 5208707 | `America/Sao_Paulo` | `goiania-go` | `Goiânia` | (Goiânia) | `Goiânia` | `Goiânia` |
| `sao-luis` | São Luís | MA | 2111300 | `America/Fortaleza` | `sao-luis-ma` | `São Luís` | `São Luís` | `São Luís` | (São Luís) |
| `belo-horizonte` | Belo Horizonte | MG | 3106200 | `America/Sao_Paulo` | `belo-horizonte-mg` | `Belo Horizonte` | `Belo Horizonte` | `Belo Horizonte` | `Belo Horizonte` |
| `campo-grande` | Campo Grande | MS | 5002704 | `America/Campo_Grande` | `campo-grande-ms` | `Campo Grande` | (Campo Grande) | `Campo Grande` | (Campo Grande) |
| `cuiaba` | Cuiabá | MT | 5103403 | `America/Cuiaba` | `cuiaba-mt` | `Cuiabá` | (Cuiabá) | (Cuiabá) | (Cuiabá) |
| `belem` | Belém | PA | 1501402 | `America/Belem` | `belem-pa` | `Belém` | `Belém` | `Belém` | (Belém) |
| `joao-pessoa` | João Pessoa | PB | 2507507 | `America/Fortaleza` | `joao-pessoa-pb` | `João Pessoa` | (João Pessoa) | (João Pessoa) | `João Pessoa` |
| `recife` | Recife | PE | 2611606 | `America/Recife` | `recife-pe` | `Recife` | `Recife` | (Recife) | `Recife` |
| `teresina` | Teresina | PI | 2211001 | `America/Fortaleza` | `teresina-pi` | `Teresina` | (Teresina) | `Teresina` | (Teresina) |
| `curitiba` | Curitiba | PR | 4106902 | `America/Sao_Paulo` | `curitiba-pr` | `Curitiba` | `Curitiba` | `Curitiba` | `Curitiba` |
| `rio-de-janeiro` | Rio de Janeiro | RJ | 3304557 | `America/Sao_Paulo` | `rio-de-janeiro-rj` | `Rio de Janeiro` | `Rio de Janeiro` | `Rio De Janeiro` | `Rio de Janeiro` |
| `natal` | Natal | RN | 2408102 | `America/Fortaleza` | `natal-rn` | `Natal` | (Natal) | (Natal) | (Natal) |
| `porto-velho` | Porto Velho | RO | 1100205 | `America/Porto_Velho` | `porto-velho-ro` | `Porto Velho` | (Porto Velho) | (Porto Velho) | (Porto Velho) |
| `boa-vista` | Boa Vista | RR | 1400100 | `America/Boa_Vista` | `boa-vista-rr` | `Boa Vista` | (Boa Vista) | (Boa Vista) | (Boa Vista) |
| `porto-alegre` | Porto Alegre | RS | 4314902 | `America/Sao_Paulo` | `porto-alegre-rs` | `Porto Alegre` | `Porto Alegre` | (Porto Alegre) | (Porto Alegre) |
| `florianopolis` | Florianópolis | SC | 4205407 | `America/Sao_Paulo` | `florianopolis-sc` | `Florianópolis` | `Florianópolis` | (Florianópolis) | `Florianópolis` |
| `aracaju` | Aracaju | SE | 2800308 | `America/Maceio` | `aracaju-se` | `Aracaju` | (Aracaju) | (Aracaju) | (Aracaju) |
| `sao-paulo` | São Paulo | SP | 3550308 | `America/Sao_Paulo` | `sao-paulo-sp` | `São Paulo` | `São Paulo` | `São Paulo` | `São Paulo` |
| `palmas` | Palmas | TO | 1721000 | `America/Araguaina` | `palmas-to` | `Palmas` | (Palmas) | (Palmas) | (Palmas) |

`ibge_code` is the 7-digit `municipio-id` of the IBGE localidades API
(`/api/v1/localidades/municipios?view=nivelado`, 5,571 rows). Every Sympla
slug of the form `<unaccented-name>-<uf>` answered a real listing; every
capital name is in uTicket's `place/city` autocomplete.

**Events listed per city and source** (what each source shows today; Sympla
is the listing's `total`, see the caveats below; uTicket counts only items
whose `place` ends in the city's UF; the three state/national sources are
filtered on `state` + folded `city`):

| city | sympla | uticket | blueticket | ticketbolt | zig |
|---|---|---|---|---|---|
| Vitória (ES) | 194 | 3 | 34 | 15 | 17 |
| Vila Velha (ES) | 190 | 7 | 5 | 1 | 6 |
| Serra (ES) | 189 | 3 | 0 | 3 | 5 |
| Cariacica (ES) | 194 | 1 | 6 | 1 | 2 |
| Viana (ES) | 198 | 1 | 0 | 0 | 0 |
| Guarapari (ES) | 201 | 1 | 0 | 0 | 6 |
| Rio Branco (AC) | 6 | 2 | 0 | 0 | 0 |
| Maceió (AL) | 136 | 0 | 1 | 1 | 2 |
| Manaus (AM) | 110 | 6 | 1 | 1 | 0 |
| Macapá (AP) | 18 | 0 | 0 | 0 | 6 |
| Salvador (BA) | 443 | 27 | 0 | 0 | 1 |
| Fortaleza (CE) | 311 | 12 | 1 | 1 | 3 |
| Brasília (DF) | 652 | 14 | 3 | 2 | 11 |
| Goiânia (GO) | 274 | 6 | 0 | 1 | 1 |
| São Luís (MA) | 86 | 4 | 2 | 21 | 0 |
| Belo Horizonte (MG) | 1,506 | 24 | 1 | 17 | 7 |
| Campo Grande (MS) | 88 | 5 | 0 | 1 | 0 |
| Cuiabá (MT) | 66 | 4 | 0 | 0 | 0 |
| Belém (PA) | 196 | 6 | 3 | 2 | 0 |
| João Pessoa (PB) | 574 | 2 | 0 | 0 | 2 |
| Recife (PE) | 350 | 7 | 3 | 0 | 1 |
| Teresina (PI) | 68 | 4 | 0 | 1 | 0 |
| Curitiba (PR) | 568 | 29 | 32 | 2 | 2 |
| Rio de Janeiro (RJ) | 2,393 | 63 | 5 | 1 | 56 |
| Natal (RN) | 113 | 1 | 0 | 0 | 0 |
| Porto Velho (RO) | 1,437 | 1 | 0 | 0 | 0 |
| Boa Vista (RR) | 3 | 0 | 0 | 0 | 0 |
| Porto Alegre (RS) | 784 | 7 | 6 | 0 | 0 |
| Florianópolis (SC) | 269 | 9 | 49 | 0 | 5 |
| Aracaju (SE) | 105 | 3 | 0 | 0 | 0 |
| São Paulo (SP) | 3,852 | 87 | 16 | 2 | 45 |
| Palmas (TO) | 33 | 1 | 0 | 0 | 0 |
| **total** | **15,607** | **340** | **168** | **73** | **178** |

**Sizing per source per cycle** (one cycle = every enabled city once, as §3
"One cycle job per source" runs it; requests per event from the ADRs: Sympla
event page + tickets = 2, uTicket `eventinfo` + `tickettype` = 2, BlueTicket
`detail` + `products/sectors` = 2, TicketBolt 1, Zig 1; listing requests:
Sympla one page walk per city, uTicket `max(1, ceil(count / 50))` per city
(34), BlueTicket one `state=<UF>` per UF (27, cached per cycle), TicketBolt
and Zig one feed per cycle):

| source | events | listing requests | event requests | requests per cycle | at 1 req/s | share of 6 h |
|---|---|---|---|---|---|---|
| sympla | 15,607 | 902 | 31,214 | 32,116 | 8 h 55 min | 149 % |
| uticket | 340 | 34 | 680 | 714 | 12 min | 3.3 % |
| blueticket | 168 | 27 | 336 | 363 | 6 min | 1.7 % |
| ticketbolt | 73 | 1 | 73 | 74 | 1 min | 0.3 % |
| zig | 178 | 1 | 178 | 179 | 3 min | 0.8 % |

**Does it fit 6 h without conditional fetch? No, for Sympla; yes, for the
other four.** Sympla needs 32,116 requests per cycle against a budget of
21,600 (6 h at 1 req/s), before the hourly 48 h refresh takes its share; the
three largest hubs alone (São Paulo, Rio de Janeiro, Belo Horizonte: 7,751
listed events) are 16,000 requests. uTicket, BlueTicket, TicketBolt and Zig
together are 1,330 requests, 22 minutes, and would fit even at ten times
today's volume. So **task 6 (conditional fetch) lands before task 8 (enable
the capitals)**; the sources' caps in §3 stay untouched. Even with it the
first cycle after enabling is a full one (no fingerprints stored yet), about
9 h for Sympla: `coalesce=True, max_instances=1` makes it skip one trigger,
not overlap. Steady state for Sympla is then the 902 listing pages plus the
changed or new events plus the 48 h window.

**Facts task 2 must build on (all Sympla unless noted):**

- **Two listing templates.** 21 cities get the generic listing (`template:
  city`, `searchDataResult`, 24 per page, `?page=N` until an empty page: what
  ADR 0003 describes). The 11 biggest capitals (São Paulo, Rio de Janeiro,
  Belo Horizonte, Brasília, Porto Alegre, Curitiba, Salvador, Recife,
  Fortaleza, Goiânia, Florianópolis) get a curated **hub page** (`matrixType:
  city`, `<title>` "Eventos em SP: …") with no `searchDataResult`: today's
  `parse_listing` raises `ValueError` on them. The hub carries four
  `collection-matrix` sliders (24 items each, own `total`) and one
  `dataSectionMoreEvents` block (`data`, `total`, `limit: 16`, `page`) that
  `?page=N` pages at **16 per page** in `global-score-norm` order
  (`svDataSectionMoreEvents: {sort, page, city: "sao-paulo"}`; page 2 has 16
  new ids, `?page=300` answers `data: []`). São Paulo is 241 pages and Rio
  150, above `MAX_LISTING_PAGES = 100`; task 2 parses both templates and
  raises or removes the cap (the empty page is the stop condition either way).
- **The generic listing is regional, not city-scoped.** The full walk of
  `vitoria-es` (194 events, 9 pages) has 105 events in Vitória, 33 in Vila
  Velha, 8 in Linhares, 7 in Serra, 7 in Aracruz, 6 in Guarapari, 3 in
  Cariacica, 2 in Viana, the rest elsewhere in ES; `cariacica-es` page 1 has
  no Cariacica event at all (22 Vitória). The six ES slugs list the same
  ~190-200 regional set (page-1 union: 61 distinct ids in 144 items), so
  today's six ES jobs fetch the same event pages up to six times per cycle
  (≈ 2,380 requests for 194 distinct events). `natal-rn` (113): 58 in Natal,
  29 in Parnamirim, 4 in PB; `joao-pessoa-pb` page 20 is Recife/PE. Hub
  "more events" pages are city-scoped as far as seen (São Paulo pages 1-2,
  Rio page 1: all in the city). Task 2 decides whether the adapter keeps
  listing items by `location.city`/`state` (halves Sympla's event fetches
  for generic cities and removes the ES overlap) or keeps today's behaviour
  and lets the fingerprint skip do it; either is a spec-§3 change to record
  in the task 2 PR, not decided here.
- **Porto Velho's 1,437** are real listing rows: one organizer's guided-tour
  time slots (`Complexo Madeira-Mamoré`, page 40 is still all of them).
  Many events per venue, few venues: cheap for the geocoder, not for the
  fetcher.
- **uTicket.** `place/city` names carry a soft hyphen (U+00AD:
  `Brasí­lia`, `São Luí­s`); `resolve()` strips it, and
  `eventsearch?city=` takes the plain name. `city=` is fuzzy: `Macapá`
  returned 15 items, all Macaé-RJ and Marabá-PA; `Viana` 1 of 2 in MA;
  `Aracaju` 3 of 6 in BA/MG. The `-<UF>` suffix filter of ADR 0006 is what
  makes the count right, now on `city.uf`. São Paulo (87) and Rio (63) are
  the only cities past one page of 50.
- **BlueTicket.** 307 events in 15 states; the capital is the bulk only in
  ES (34 of 45), PR (Curitiba 32 of 59), SP (16 of 28); SC's 96 are
  Florianópolis 49 plus the coast; RS's 46 are Novo Hamburgo 18, Porto
  Alegre 6. Twelve states answered `[]`. `cityName` uses accents
  (`Maceió`, `São Luís`, `Florianópolis`).
- **TicketBolt.** 104 events nationally, all `Publicado`, one page
  (`hasNextPage: false`): the 100 of ADR 0008 was the count that day, not a
  cap. Cities are Title Case (`Rio De Janeiro`, `Juiz De Fora`): match
  folded. São Luís (21) is now its largest city; Manaus has 1, which gives
  task 2 its `America/Manaus` wall-time case (Zig has no Manaus event today).
- **Zig.** 294 feed items (17 states), all live; 178 in the 32 cities (Rio
  56, São Paulo 45, Vitória 17, Brasília 11). Coordinates on 3 of 294.
  `timezone`: 290 `America/Sao_Paulo`, 3 `Brazil/DeNoronha` (Fernando de
  Noronha, PE), 1 `Brazil/East`; dirty rows: `state` `"."` (2) and `"sc"`
  (1), `city` `Sao Paulo` (3) and `SAO SEBASTIÃO`: fold before matching.

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
  see from us. **Task 1 result (2026-09-22): before.** Sympla alone is
  32,116 requests per cycle (8 h 55 min) against a 6 h budget of 21,600;
  the other four sources fit in 22 minutes (§2, "Task 1 recon").
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
