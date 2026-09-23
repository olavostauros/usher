# 0012. Removal is per listing and per city; the event follows its last listing

Date: 2026-09-23. Status: accepted (Phase 3 national, task 4;
`docs/specs/phase-3-national.md` §3 "Removal per listing, per city").

## Context

Until now `mark_missing` ran only after a crawl of one whole source (no
`--city`, no `--limit`) and flipped the *event* to `removed_from_source` as
soon as that source stopped listing it. Two problems came with national
coverage:

- The scheduler crawls one city at a time, and a per-city run never called
  `mark_missing`, so scheduled crawls removed nothing at all.
- A merged event (ADR 0010) that one source dropped and another still listed
  was marked removed, then revived by the other source's next crawl: a
  version pair per cycle (ADR 0010, "Consequences").

Migration `0003` added `source_listings.city` and `source_listings.removed_at`
for this.

## Decision

- **The listing remembers its city.** `upsert_event(..., city=<slug>)` stamps
  the registry slug the listing was discovered under; a run with no city
  (every city, or the hourly refresh) keeps the stored one. A listing seen
  under two cities carries the last one.
- **Removal is scoped to what was looked at.**
  `mark_missing(session, source, city, seen)` runs after every complete
  crawl: no `--limit`, no refresh URL list, zero errors, and at least one
  event fetched. It considers the source's listings of that city whose event
  is active and upcoming and whose `removed_at` is empty; each one not in
  `seen` gets `removed_at = now`. `city = None` (a run over every city) puts
  every listing of the source in scope, including rows stored before `0003`,
  which have no city yet.
- **The event follows its last listing.** An event becomes
  `removed_from_source`, with an `event_versions` row, only when none of its
  listings on any source is left without `removed_at`. A secondary (or
  primary) listing disappearing while another remains writes nothing on the
  event.
- **Seen again is back.** Every upsert clears `removed_at`; the existing
  status rules revive a removed event (primary: `next_status`; secondary:
  back to `scheduled`), with a version.
- `mark_missing` returns, and `crawl_runs.removed` counts, **listings**
  removed, not events.

## Consequences

- ADR 0010's churn note is closed: a merged event survives while any source
  lists it.
- Listings stored before `0003` get their city on the next crawl that sees
  them. One that disappeared before that stays without a city, so only a
  whole-source crawl (`evcrawl crawl <source>` without `--city` or
  `--limit`, a manual run) can remove it; it otherwise simply ages out
  when the event starts.
- A listing shown under two cities is stamped by whichever crawl saw it last,
  so only that city's crawl can remove it. If it disappears from that city
  but stays under the other, it can be removed and then revived by the other
  city's next crawl: one version pair, only for listings that change city.
- A city whose complete crawl finds zero events removes nothing (the
  `fetched > 0` guard): an empty listing is more likely a broken page than a
  city with no events. Spec 3's `removal` SLA measures what this leaves.
- Rows are never deleted; `removed_at` is history the listing carries.
