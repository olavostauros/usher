# 0021. How Ingresse data is acquired

Date: 2026-10-09. Status: accepted (`docs/SPECIFICATION.md` §8.3, task 1).

## Context

Source expansion wave 1 starts with Ingresse (`www.ingresse.com`), a
mainstream platform strong in shows, club nights and festivals. Recon on
2026-10-09 from the host, with the project's User-Agent at 1 req/s.

The §8.3 gate:

1. **robots.txt.** `www.ingresse.com/robots.txt` is empty (200);
   `api-site.ingresse.com/robots.txt` is 404, which the fetch layer reads
   as allow-all.
2. **Terms of use** (`sobre.ingresse.com/terms-of-service`, last updated
   2024-01-24): nothing on robots, crawling, scraping or automated access.
   The only related clause forbids copying or distributing Ingresse's
   brand, interface and software, which this project does not do: it stores
   public event data with the source URL.
3. **Plain client.** Every request below answered 200 to the project's
   User-Agent with no cookie, key or extra header. No bot wall, no waiting
   room.
4. **Listing and event read.** Both are a public JSON API (below).
5. **Overlap.** Measured in the `dev` container before the PR (the PR body
   has the numbers).

The site is a client-rendered Next.js app. Reading its JS chunks gives the
API it calls, `https://api-site.ingresse.com`:

- `GET /events/search?company_id=1&iso_code=BRA-<UF>&size=40&offset=<n>&order_by_date=true`
  lists one state. `offset` counts items. `pagination.total_pages` is wrong:
  it said 2 for São Paulo, which had 5 pages (173 events) on 2026-10-09. A
  short page is the last one. A state without events, and the page after
  the last one, answer **404** `{"detail": "No events found"}`, or sometimes
  200 with the same body. Items have `id`, `slug`, `title`, `poster`,
  `session` (only the next one: `dateTime` with offset, `timezone`,
  `status`), `place` (`city`, `state` (UF), `name`, `street`, `zip`,
  `location.lat/lon`), `categories[]` (`slug`, `name`), `producerId`,
  `delta` (a search score) and `trackingTags` (the organizer's ad ids).
  First pages on 2026-10-09: SP 173 events (130 in São Paulo), RJ 40+, SC
  40+, BA 35, MG 28, RS 28, PR 12, CE 10; 9 states had none.
- `GET /events/<slug>` adds `description` (HTML), every `sessions[]` entry
  (start only, past ones included: one season ran from 2025-10-22 to
  2026-10-31 over 37 sessions), `timezone` (IANA), the full `place` and
  `eventDisclaimer` (the platform's sales rules, the same on every event).
- The event page is `https://www.ingresse.com/<slug>/`, the URL the site
  links to.
- **Prices** are not on `api-site`. The page reads them from
  `api-embedstore.ingresse.com/api/v1/event/<id>/session/0/tickets`, which
  answers 422 without an `apikey`. Ingresse's site code includes a key for
  it.

## Decision

- `discover()` fetches each state's listing once per adapter instance,
  page by page until a short page, an empty page, a page with no new id or
  a 404, and keeps the items whose `place.state` is the city's UF and whose
  `place.city` folds to the registry name. The fingerprint (ADR 0013) is the
  whole item minus `delta` and `trackingTags`.
- `fetch()` calls `/events/<slug>`. `starts_at` is the earliest session.
  `ends_at` is the last session's start when there are several sessions,
  and none for a single session, which the API's window rule treats as
  open-ended. The timezone is the event's own when it is a known IANA zone,
  otherwise the UF's. Coordinates come from `place.location`. The image is
  `poster.large`. The category is the first `categories[].slug` that is not
  a promo tag (`nao-fique-de-fora`), mapped in `normalize.py`. `raw` keeps
  the event minus `eventDisclaimer` and `trackingTags`.
- **No prices.** The owner chose on 2026-10-09 not to use the key from
  Ingresse's site code. Every record is `price_lots_pending`. Asking
  Ingresse for access, or the owner reversing that choice, reopens this.
- No extra headers. Rate limit 1 req/s, 5 retries (the default).
- Registry key: the city's display name, the same as the other JSON sources.

## Consequences

- A full cycle costs one request per listing page per state, plus one per
  new or changed event, or per event starting within 48 h (ADR 0013). On
  2026-10-09 that was about 20 listing pages nationally and a few hundred
  events in the 32 cities.
- Long seasons show their first session's date, as uTicket's do.
- Events Ingresse lists without categories (for example Brasil Game Show)
  land in `uncategorized`.
