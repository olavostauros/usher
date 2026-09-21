# 0010. Cross-source dedupe: when listings merge and which fields win

Date: 2026-09-21. Status: accepted (Phase 2 task 9; `docs/specs/phase-2-sources.md`
§4 last bullet).

## Context

Phase 1 shipped the dedupe scaffold: `find_candidates` in
`src/evcrawl/pipeline/dedupe.py` finds events that look like an incoming
listing and `flag_duplicates` marks both sides `dedupe_review = true`; nothing
is merged, every listing gets its own `events` row. With five sources live,
the same show is regularly on two of them (AGENTS.md §7: "an event may exist
on several platforms; `events` is the canonical merged record"), and the PWA
would show it twice. This ADR records the merge rule that
`src/evcrawl/pipeline/upsert.py` implements and what it leaves to a human.

## Matching keys (unchanged from AGENTS.md §7)

A candidate for an incoming normalized listing *n* is an `events` row with:

- the same `title_normalized` (lowercase, unaccented, whitespace collapsed);
- `starts_at` within ±2 h of `n.starts_at` (`START_TOLERANCE`);
- a venue within 300 m of `n`'s coordinates (`ST_DWithin` on
  `venues.location`, `VENUE_RADIUS_M`); when either side has no coordinates,
  the same `address_normalized`; online events match on title and time alone.

## Confidence criteria

The listing is linked to a candidate only when there is **exactly one** and
it is *confident* (`dedupe.is_confident`):

1. **Cross-source only.** The candidate carries no listing from `n.source`.
   The same platform showing one event twice is a duplicate posting or a
   second session, not a cross-source match; it stays separate and flagged.
2. **Not already under review.** A candidate with `dedupe_review = true` is
   unsure by definition; the new listing does not compound it.

Zero candidates: a new event. One unsure candidate or several candidates: a
new event, flagged together with every candidate (`dedupe_review = true`,
the `dedupe.review` warning), exactly as in Phase 1. Matching happens **only
the first time a source shows a listing**; a listing that already has its
event is never moved to another one. On re-crawl, look-alikes that appeared
since flag both sides for review (`flag_duplicates`), as before.

## Field precedence: first seen wins, empty fields filled from any source

- The event's **primary listing** is its oldest one (`first_seen_at`, then
  `id`). Its content is the event's content: title, start, timezone,
  online flag, `content_hash`, and — when present — description, end,
  image, venue, organizer, category and price lots. A primary that changes
  updates the row and writes an `event_versions` row with the usual status
  transition (`next_status`).
- Every other listing is **secondary**. When it is linked (and again
  whenever its own content changes) it fills, and only fills, what the row
  still lacks: `description`, `ends_at`, `image_url`, `venue`, `organizer`,
  `category` (where `uncategorized` counts as empty) and `price_lots` (where
  an empty list counts as empty; a pending `A partir de` lot does not). It
  never overrides a value, never changes the title or the start, never
  changes the status except to revive a row that was `removed_from_source`
  when the secondary still lists it.
- Once an event has secondaries, an empty value from the primary no longer
  blanks a filled field. A single-listing event still mirrors its source
  exactly, as in Phase 1.
- The merge writes an `event_versions` row (`version + 1`, content
  `{"reason": "listing merged", "source", "external_id", "source_url",
  "filled": {...}}`); a later fill writes one with `"reason": "listing
  filled"`; a secondary re-crawl that fills nothing writes none, so a second
  run of every source is idempotent.
- `raw_snapshots` stay per listing; `source_listings.source_url` keeps the
  attribution of every platform (AGENTS.md §10).

`UpsertResult` gained `merged` and `review`; `CrawlSummary` and the
`crawl.done` log report `merged` next to `created` and `review`.

## Consequences

- Existing rows are not re-merged: events created before this rule, one per
  listing, stay as they are and get `dedupe_review` on their next re-crawl
  if a look-alike exists. Merging them is a manual clean-up, not a crawl.
- `mark_missing` (full crawl of one source, no `--city`, no `--limit`) still
  marks an event removed when that source drops its listing, even if
  another source keeps it; the other source's next crawl revives it. This
  can churn a version per cycle for such events. The scheduler runs
  per-city crawls, which never call `mark_missing`, so today it only
  happens on a manual national crawl. Revisit with Phase 3's freshness
  work.
- Title matching is exact on the normalized form: "Eclipse Festival" and
  "Eclipse Festival 2026" are two events. Fuzzy titles are a Phase 3
  question; the rule here prefers a missed merge to a wrong one.
