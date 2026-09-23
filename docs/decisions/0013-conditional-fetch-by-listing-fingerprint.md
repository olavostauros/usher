# 0013. Conditional fetch: an unchanged listing item skips the event page

Date: 2026-09-23. Status: accepted (Phase 3 national, task 6;
`docs/specs/phase-3-national.md` §3 "Conditional fetch").

## Context

Task 1's sizing put Sympla at 32,116 requests per cycle over the 32 registry
cities, against a budget of 21,600 (6 h at the unchanged 1 request/second),
before the hourly refresh takes its share. Every other source fits in 22
minutes. Raising a rate limit is not an option (AGENTS.md §10, §11); the cycle
has to skip work instead. Every source's listing already returns, per event,
an item that changes when the event changes (Sympla's search object,
uTicket's search item with `updatedAt`, BlueTicket's listing item, TicketBolt's
query item, Zig's feed item), so a second sight of an identical item means the
event page has nothing new. Migration `0003` (task 3) added
`source_listings.listing_fingerprint` and `crawl_runs.skipped` for this.

The same mechanism absorbs Sympla's regional overlap (ADR 0003 addendum): the
six Greater Vitória slugs list the same ~190 events, which were fetched up to
six times per cycle.

## Decision

- **`discover()` yields `Discovered(url, external_id, fingerprint)` or a plain
  `str`.** The spec wrote `Discovered(url, fingerprint)`; the external id is
  added because `source_listings` is keyed on `(source, external_id)` and a
  skipped item never produces the record that carries the id. Every adapter
  has it in the listing item (Sympla: the id in the URL, `bileto-<id>` for
  bileto entries; uTicket, BlueTicket and Zig: `item["id"]`; TicketBolt:
  `item["_id"]`), and the adapter tests assert it equals the fetched record's
  id. A plain `str` is fetched as before, always.
- **The fingerprint** is `sources.base.fingerprint(item, drop=...,
  drop_prefixes=...)`: sha256 hex of the canonical JSON (sorted keys, same
  form as `raw_snapshots`) of the listing item minus its volatile keys.
  What each adapter leaves out, on top of the contact fields it already
  strips (ADR 0011):

  | source | dropped | kept on purpose |
  |---|---|---|
  | Sympla | `global_score` (ranking) | dates, name, location, organizer name |
  | uTicket | nothing | `updatedAt`, the API's edit stamp |
  | BlueTicket | `eventOrder`, `distances`, `relatedEvents`, `display*` | `onSale`, `date`, `eventTime`, `venue` |
  | TicketBolt | `visualizacoes` (views), `featuredOnHomepageHero`, `_creationTime` | `eventDate`, `status`, `is_cancelled`, `location` |
  | Zig | `verification_code`; `organization` reduced to id, name, slug | dates, `is_closed`, `event_location` |

  A key that turns out volatile only costs a fetch, never a miss.
- **The skip rule** (`pipeline.upsert.skip_unchanged`, called by
  `run_crawl` before the fetch): the event page is skipped only when the
  stored fingerprint equals the new one **and** the listing is not
  `removed_at` **and** its event is active **and** the event starts later
  than `REFRESH_WINDOW` (48 h) from now. Everything else fetches: no stored
  fingerprint yet, a changed item, an event about to start (the refresh
  window always sees the page), a removed listing that reappears (the
  upsert revives the event), a lookup error (logged as `crawl.skip_failed`,
  counted as an error, fetched anyway). A dry run has no session and always
  fetches.
- **A skipped listing counts as seen.** The skip stamps `last_seen_at`,
  `last_crawl_run_id` and the run's city, commits, and adds the id to the
  run's `seen` set, so `mark_missing` (ADR 0012) treats it exactly like a
  fetched one. An all-skipped complete run still sweeps the listings the
  source dropped (`fetched + skipped > 0` is the guard now), and is not a
  failed run (`discovered > 0 and fetched == 0 and skipped == 0` is).
- **The fingerprint is stored by the upsert** that follows a fetch, and only
  then: `upsert_event(fingerprint=None)` keeps what is stored, so the hourly
  refresh (plain URLs from the database) never clears it.
- **Counts:** `CrawlSummary.skipped`, `crawl_runs.skipped`, `skipped=` in the
  `crawl.done` and `cycle.done` logs, `upsert.listing_skipped` per listing.

## Consequences

- Steady state for Sympla is the listing page walk (902 pages) plus the
  changed or new events plus the 48 h window; the first cycle after task 8
  enables the capitals is still a full one (about 9 h, `coalesce=True`
  skips the overlapping trigger).
- Sympla's bileto entries are built from listing data and cost no request;
  a skip saves only the upsert there. Verified on the shared db
  (2026-09-23): `crawl sympla --city vitoria --limit 5` twice gives
  `fetched=5 skipped=0` then `fetched=0 skipped=5`; the same for `crawl zig`.
- A `Discovered` is a frozen dataclass, not a `str`: tests collect `.url`.
  Fake adapters that yield `str` keep working and are never skipped.
- An event that changes on its page without its listing item changing
  (rare: a description edit that the listing does not carry) is seen at the
  latest when it enters the 48 h window. Spec 3's freshness SLAs are
  measured on `crawl_runs`, which a skip still writes.
- The comparison is a `SELECT` per discovered listing; on the shared db it
  costs nothing next to the request it replaces.
