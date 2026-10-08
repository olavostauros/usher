# Usher events API, v1

> **Draft, 2026-09-24.** This is the contract described in
> `docs/SPECIFICATION.md` §7. Every endpoint below is built and served
> by the `api` compose service (`ops/host/README.md` "Events API") at
> the public URL `https://api.eventolivre.com` (below). The
> machine-readable version is [`openapi.json`](openapi.json) in this
> folder, generated from the code and checked by a test. If the two ever
> disagree, `openapi.json` wins.

The API gives the Usher PWA the upcoming events the Evento Livre crawler
collects from Brazilian ticketing platforms (Sympla, uTicket, BlueTicket,
TicketBolt, Zig) in the 27 capitals and Greater Vitória. It is read-only and
public-data-only, and there are no user accounts. Tickets are sold by the
source platforms, so every event carries links to them.

## Where it is

| Stage | Base URL | Who can reach it |
|---|---|---|
| Development (after spec task 4) | `http://<host-tailnet-name>:8000` | Team members on the Tailscale network |
| Public (spec task 5) | `https://api.eventolivre.com` | Anyone; browsers only from `https://eventolivre.com` (CORS) |

The interactive docs are at `/v1/docs` on the development URL only; the
public URL answers 404 there.

Every response on either URL, the 429 of the rate limiter included, carries
these CORS headers when the request's `Origin` is `https://eventolivre.com`,
and no `Access-Control-Allow-Origin` for any other origin:

```
Access-Control-Allow-Origin: https://eventolivre.com
Access-Control-Allow-Methods: GET
Access-Control-Allow-Headers: accept
Access-Control-Expose-Headers: retry-after
```

No wildcard origin, no credentials.

## Conventions

- All endpoints are `GET` and return `application/json`. There is no
  authentication in v1.
- **Times** are ISO 8601 in UTC (`2026-10-03T23:00:00Z`). Each event also
  has its IANA `timezone` (`America/Sao_Paulo`, `America/Manaus`, …).
  Display local time in the event's zone, not the phone's.
- **Money** is a decimal string (`"45.00"`) plus `currency` (`BRL`). Never
  parse it as a float for arithmetic.
- **Coordinates** are WGS 84 `lat`/`lon` with 6 decimals.
- **`null` means unknown.** Missing values are never replaced with a guess.
- **Ids** are integers and stable. An event keeps its id for as long as it
  exists, and events are never deleted.
- **Pagination** uses a cursor: pass `next_cursor` from the previous page as
  `cursor`. When `next_cursor` is `null`, there are no more pages. There are
  no page numbers and no totals.
- **Caching:** event lists are `max-age=300`; event details, categories and
  cities are `max-age=900`. All carry an `ETag`. Send `If-None-Match` to get
  a `304` back.
- **Errors** look like `{"error": {"code": "invalid_parameter",
  "message": "...", "field": "radius_km"}}`, with HTTP 400/422 (bad input),
  404 (unknown id), 410 (event no longer active), 429 (rate limited; see
  `Retry-After`) or 503 (`db_unavailable`: retry later). `field` is present
  only when one parameter is at fault.
- **Rate limit:** 60 requests a minute per client address on `/v1/*`. A
  `429` carries `Retry-After` in seconds. Cache what you can (see above).

## Endpoints

### `GET /v1/events`: search, "events near me"

Either `lat` and `lon`, or `city`, is required.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `lat`, `lon` | number | none | The user's position. Use both or neither. |
| `radius_km` | number | `25` | 1 to 100. Needs `lat`/`lon` (422 otherwise). |
| `city` | string | none | A registry slug from `/v1/cities`, e.g. `sao-paulo`, `vitoria`. Events listed under that city; an unknown slug is a 422. |
| `from`, `to` | date or datetime | now, `from` + 30 days | Events that start before `to` and have not ended by `from`: at their `ends_at`, or six hours after `starts_at` when the source gave no end, so with the default `from` an event running now is listed. A date (`2026-10-07`) is that whole UTC day, so `to=2026-10-07` includes the 7th; a datetime is an instant (naive means UTC). Max span 180 days: with the default `from`, the widest `to` is the UTC date 179 days ahead. |
| `category` | string, repeatable | all | Slugs from `/v1/categories`; up to 10. |
| `free` | boolean | any | `true`: only events with a free ticket. `false`: only events without a known free ticket (paid ones, and ones with no prices). |
| `online` | `include` \| `only` \| `exclude` | `exclude` with `lat`/`lon`, else `include` | Online events have no venue. With `lat`/`lon` and `include`, they come after every located event; with `only`, the radius is ignored. |
| `q` | string | none | Every word must appear in the title, ignoring accents, case and punctuation; up to 6 words. |
| `sort` | `distance` \| `starts_at` | `distance` with `lat`/`lon`, else `starts_at` | `distance` needs `lat`/`lon`. Ties break on `id`. |
| `limit` | integer | `20` | 1 to 100. |
| `cursor` | string | none | `next_cursor` of the previous page, with the same other parameters. A cursor from another `sort` is a 422. |

```http
GET /v1/events?lat=-20.3155&lon=-40.3128&radius_km=10&from=2026-10-01&to=2026-10-07&limit=2
```

```json
{
  "data": [
    {
      "id": 48213,
      "title": "Festival de Jazz de Vitória",
      "starts_at": "2026-10-03T23:00:00Z",
      "ends_at": "2026-10-04T03:00:00Z",
      "timezone": "America/Sao_Paulo",
      "is_online": false,
      "image_url": "https://images.example-source.com/…/banner.jpg",
      "category": {"slug": "music", "name": "Música"},
      "venue": {
        "id": 912,
        "name": "Teatro Municipal",
        "address": "Rua …, Centro, Vitória - ES",
        "city": "Vitória",
        "uf": "ES",
        "lat": -20.319812,
        "lon": -40.338104
      },
      "distance_m": 2614,
      "price": {"min": "0.00", "max": "80.00", "currency": "BRL", "is_free": true},
      "sources": [
        {"source": "sympla", "url": "https://www.sympla.com.br/evento/…"}
      ],
      "sessions": 1
    }
  ],
  "next_cursor": "eyJ2IjoxLCJrIjoiZCIsImlkIjo0ODIxMywiZCI6MjYxNH0"
}
```

`distance_m` is metres from `lat`/`lon`, rounded; it is `null` when no
`lat`/`lon` were sent, and for online events. `venue` is `null` for online
events, and `price` is `null` when the source published no prices. A list
item carries no `description` or `price_lots`; get those from the detail
endpoint.

**Recurring events are one row.** Some sources list every session of a
recurring event as its own event (a museum's guided tour, one per hour for
months). The list groups events at the same venue whose titles are the same
once a leading time of day (`17h`, `9h30`, `17:00`, then an optional
`horário local`) is removed, and returns the group's next session in the
window, with `sessions` counting the group's sessions in the window
(`1` for an ordinary event). The detail endpoint serves every session by
its own id; the counts in `/v1/categories` and `/v1/cities` count a series
once, like the list. To show "and 46 more sessions", use `sessions - 1`.

### `GET /v1/events/{id}`: one event

This is the list item, without `distance_m` and `sessions`, plus
`description`, `organizer` (`{name, url}` or `null`), `price_lots`
(`[{name, amount, currency}]` in the source's order, `[]` when the source
published none), `sources` (every platform listing it; the first is the
oldest) and `updated_at`.

- `404`: no event has ever had this id.
- `410`: the event exists but is no longer active. The body is
  `{"error": {"code": "gone", "message": "…", "status": "cancelled"}}`, where
  `status` is one of `cancelled`, `ended` or `removed_from_source`. Use it to
  explain an old bookmark.

### `GET /v1/categories`

`{"data": [{"slug": "music", "name": "Música", "events_upcoming": 812}, …]}`.
`uncategorized` exists and holds events the source did not classify.
`events_upcoming` is how many rows `/v1/events?category=<slug>` returns
for the category over every city and the widest window (below), a
recurring series counted once.

### `GET /v1/cities`

`{"data": [{"slug": "vitoria", "name": "Vitória", "uf": "ES",
"timezone": "America/Sao_Paulo", "events_upcoming": 214}, …]}`: the 32
cities crawled today.

**`events_upcoming` is the length of the city's list.** It is the number
of rows `GET /v1/events?city=<slug>&to=<today + 179 days>` returns over all
its pages: events that have not ended (at `ends_at`, or six hours after
`starts_at` when the source gave no end) and start before 00:00 UTC of the
180th day from today (UTC), a recurring series counted once. Show it as
"how many you will see"; it is not a count of sessions, and it never
includes an event the list would not.

### `GET /healthz`

`{"ok": true, "git_sha": "e9d7c0f", "db": "ok"}`. It is for uptime checks,
not for the PWA.

## What the data is, and is not

- **Coverage:** Greater Vitória (6 municipalities) and the other 26 capitals.
  Events outside those cities appear only when a source lists them under
  one of them.
- **Freshness:** every source is re-crawled every 6 hours, and events
  starting within 48 hours are re-checked every hour. An event may still
  show for up to a few hours after its source removed or cancelled it.
- **Duplicates:** an event on several platforms is merged into one id with
  several `sources`. When the match is uncertain, the listings stay separate,
  so the same show can occasionally appear twice.
- **Location:** events whose venue could not be geocoded have
  `venue.lat`/`lon` `null`. They appear in `city` searches but not in radius
  searches.
- **Not available:** ticket availability or sold-out state, seat maps,
  attendee data, and organizer contacts (removed on purpose). Images are the
  source's URLs. We do not host or resize them.
- **Privacy:** the API does not store or log the positions or IP addresses
  it receives. Keep the user's profile in the PWA. The API needs only the
  filters.

## Changes

A breaking change (renaming or removing a field, changing a type or a
default) ships as `/v2`, and `/v1` stays until the PWA has moved. Adding a
field or an optional parameter is not breaking, so clients must ignore
fields they do not know.
