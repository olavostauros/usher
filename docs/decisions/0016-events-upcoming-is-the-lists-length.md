# 0016. `events_upcoming` is the length of the list, and a running event stays listed

Date: 2026-09-25. Status: accepted (Phase 4, `docs/specs/phase-4-api.md`
§3 "Recurring sessions collapse in lists"; migration `0010`).

## Context

The PWA's place picker shows each city as "Vila Velha · ES (18 eventos)",
the number being `events_upcoming` from `/v1/cities`, and then lists
`/v1/events?city=<slug>` with the widest `to` the API accepts. People read
the number as "how many I will see", and on 2026-09-25 it disagreed with
the list three ways:

- `api_city_counts` counted every row of `api_events`, so a recurring
  series (one event per hourly slot of a museum tour) was one count per
  session but one row in the list: Porto Velho showed 1407 next to 60 rows.
- `api_events` keeps an event for six hours past its end, so the detail
  endpoint can still answer an old link, while the list dropped an event at
  its end (Boa Vista: 5 next to 4).
- `api_events` has no horizon, while the list stops at 180 days (two of
  Cuiabá's 76).

There was also an open question: with the default `from` (now), an event
with an end time in the future was listed while it ran, but an event
without an end time left the list the moment it started, though the view
kept it (as "probably still on") for six hours.

## Decision

- **A row is in the window when it starts before `to` and has not ended by
  `from`**: at its `ends_at`, or six hours after `starts_at` when the source
  gave no end (`OPEN_ENDED_HOURS` in `evcrawl.api.events`). With the default
  `from` an event running now is listed, whether or not its end is known,
  and an event that has ended is not. `api_events` itself keeps its
  six-hour grace after the end, so `/v1/events/{id}` still answers a link
  opened just after the show.
- **`events_upcoming` counts what the list shows.** `api_categories` and
  `api_city_counts` count the rows `/v1/events` returns with the default
  `from` and the widest `to` (the UTC date 179 days ahead, inclusive, so
  starting before 00:00 UTC of the 180th day), a series (same venue, same
  title without its time-of-day prefix) once. The category count spans
  every city. The views inline the list's predicate, horizon and series
  prefix from `evcrawl.api.events`; a test holds the live view to those
  constants, so changing one there means a new migration.
- The response shape does not change: `events_upcoming` stays a required
  integer, and no session count is added until a client asks for one.

## Consequences

- For every city, `events_upcoming` equals the rows of
  `/v1/events?city=<slug>&to=<today + 179 days>` over all pages; a test
  walks every seeded city and category to hold it, and the check in the
  PWA's request (a script over the live API) passes by construction.
- A count and a list are cached (900 s and 300 s) and each takes "now" when
  it runs, so around the end of an event, or at the UTC day boundary, the
  two can differ by one until the caches turn over.
- The list's default gains events that started up to six hours ago without
  an end time. They sort first under `starts_at`, which is the "happening
  now" a person expects.
