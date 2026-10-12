# 0022. A gone event page removes its listing, and is not a run error

Date: 2026-10-12. Status: proposed (production health check of 2026-10-12).

## Context

On 2026-10-12 the Grafana Freshness board showed Sympla at `errors` in 20 of
its 33 city cells. Every error line was the same: `fetch
https://www.sympla.com.br/evento/<slug>/<id>`, `FetchError ... (status=404)`.
Sympla redirects those event pages to `/404`: the organiser deleted or
unpublished the event. The city listing still carried some of them for a
while (its search index lags the event page; one listing that 404'd at
12:00 UTC was no longer listed a few hours later). Every crawl error line on
the board was a 404; nothing 403'd, 429'd or timed out, so it is not a
rate-limit problem. Errors have been there since the first national cycle
on 2026-09-22 (`v_crawl_runs_hourly`). The hourly refresh fails 46-59 times
an hour next to ~370 good fetches, a slowly shrinking set that fits stored
listings of deleted events retried until they start; its error lines are not
in the views, so the refresh numbers after this release are the check.

The cost was not the red cells. A run with any error is not "complete", so
ADR 0012's `mark_missing` never ran for those cities: events Sympla had
dropped stayed `scheduled` in the database and in the public API, and the
hourly refresh retried the same dead pages until each event's start time
passed.

## Decision

- When an adapter's `fetch(url)` fails with a `FetchError` whose `url` is
  the listing's own URL and whose status is **404 or 410**, the source has
  answered that the event is gone. `run_crawl` counts it as `gone` (in the
  summary and the `crawl.done` log), not as an error, and `mark_gone`
  stamps `removed_at` on the stored listing; the event follows its last
  listing exactly as in ADR 0012 (`event_versions` reason `source page
  gone`). This applies to a crawl and to the hourly refresh, which until now
  could never remove anything.
- **Guard:** this holds only when the same run fetched or skipped (ADR
  0013) more listings than it found gone. Otherwise every gone page is an error, as before, and nothing
  is removed: a source that moves its URLs changes every listing
  fingerprint, so nothing is skipped and every page 404s; that, or a 404
  page served for everything, must turn the run red, not empty the
  catalogue (the same spirit as ADR 0012's `fetched > 0` guard). Skipped
  listings count because a steady-state city fetches only its new and
  changed listings, and the gone ones are mostly new.
- A 404 on any *other* URL (an API, a tickets endpoint, a listing page) is
  unchanged: the adapter handles it or it is an error. So the rule covers
  the adapters whose `fetch` requests the listing URL itself (Sympla, Zig,
  TicketBolt); uTicket and BlueTicket fetch an API and keep their 404s as
  errors until their adapters say otherwise.
- The stored listing is matched by `source_url` or by the `external_id`
  discovery yielded (Sympla stores the page's `newUrl`).
- The hourly refresh no longer re-fetches listings already removed; a
  removed listing that is listed again comes back through the cycle.
- Seen again is still back: a listing removed this way that the source lists
  again, with a page that answers, is revived by the next upsert (ADR 0012).

## Consequences

- The Freshness verdict `errors` again means "something on our side or the
  source's failed", not "the source deleted an event": a run whose only
  failures are gone pages is `fresh`. No threshold changed; a 5xx, a 429
  after retries, a timeout, a waiting room or an unparsable page is still an
  error.
- Removal per city works again for Sympla, so the API stops serving events
  Sympla has dropped, and the refresh stops re-fetching dead pages every
  hour.
- `gone` is not a `crawl_runs` column (adding one is a schema change of a
  core table, AGENTS.md §11): the stored ones show up in `removed`; pages
  that were gone before we ever stored them are only in the logs
  (`crawl.page_gone`). A column can follow if the dashboard needs it.
- A source that answers 404 for a page that is only *temporarily* hidden
  loses the listing until its next listing walk sees it again with a working
  page. That is the same trade ADR 0012 made for a listing missing from one
  walk.
