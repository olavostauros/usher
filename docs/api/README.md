# Usher events API, v1

> **Draft, 2026-09-24.** Nothing is served yet. This is the contract being
> built by `docs/specs/phase-4-api.md`. Once task 2 lands, the machine-readable
> version is [`openapi.json`](openapi.json) in this folder, generated from
> the code and checked by a test. If the two ever disagree, `openapi.json`
> wins.

The API gives the Usher PWA the upcoming events the Evento Livre crawler
collects from Brazilian ticketing platforms (Sympla, uTicket, BlueTicket,
TicketBolt, Zig) in the 27 capitals and Greater Vitória. It is read-only and
public-data-only, and there are no user accounts. Tickets are sold by the
source platforms, so every event carries links to them.

## Where it is

| Stage | Base URL | Who can reach it |
|---|---|---|
| Development (after spec task 4) | `http://<host-tailnet-name>:8000` | Team members on the Tailscale network |
| Public (after spec task 5) | not decided yet | Anyone |

The interactive docs are at `/v1/docs` on the development URL.

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
- **Caching:** lists are `max-age=300` and details `max-age=900`, both with
  `ETag`. Send `If-None-Match` to get a `304` back.
- **Errors** look like `{"error": {"code": "invalid_parameter",
  "message": "...", "field": "radius_km"}}`, with HTTP 400/422 (bad input),
  404 (unknown id), 410 (event no longer active), 429 (rate limited; see
  `Retry-After`) or 503 (database unavailable).

## Endpoints

### `GET /v1/events`: search, "events near me"

Either `lat` and `lon`, or `city`, is required.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `lat`, `lon` | number | none | The user's position. Use both or neither. |
| `radius_km` | number | `25` | 1 to 100. Needs `lat`/`lon`. |
| `city` | string | none | A registry slug from `/v1/cities`, e.g. `sao-paulo`, `vitoria`. |
| `from`, `to` | date or datetime | now, now + 30 days | Events that overlap the window. Max span 180 days. |
| `category` | string, repeatable | all | Slugs from `/v1/categories`. |
| `free` | boolean | any | `true` returns only events with a free ticket. |
| `online` | `include` \| `only` \| `exclude` | `exclude` with `lat`/`lon`, else `include` | Online events have no venue. |
| `q` | string | none | Matches words in the title, ignoring accents and case. |
| `sort` | `distance` \| `starts_at` | `distance` with `lat`/`lon`, else `starts_at` | |
| `limit` | integer | `20` | 1 to 100. |
| `cursor` | string | none | From the previous page. |

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
      ]
    }
  ],
  "next_cursor": "eyJkIjoyNjE0LCJpZCI6NDgyMTN9"
}
```

`distance_m` is present only when `lat`/`lon` were sent. A list item carries
no `description` or `price_lots`; get those from the detail endpoint.

### `GET /v1/events/{id}`: one event

This is the list item plus `description`, `organizer` (`{name, url}` or
`null`), `price_lots` (`[{name, amount, currency}]` in the source's order),
`sources` (every platform listing it; the first is the oldest) and
`updated_at`.

- `404`: no event has ever had this id.
- `410`: the event exists but is no longer active. The body is
  `{"error": {"code": "gone", "status": "cancelled"}}`, where `status` is one
  of `cancelled`, `ended` or `removed_from_source`. Use it to explain an old
  bookmark.

### `GET /v1/categories`

`{"data": [{"slug": "music", "name": "Música", "events_upcoming": 812}, …]}`.
`uncategorized` exists and holds events the source did not classify.

### `GET /v1/cities`

`{"data": [{"slug": "vitoria", "name": "Vitória", "uf": "ES",
"timezone": "America/Sao_Paulo", "events_upcoming": 214}, …]}`: the 32
cities crawled today.

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
