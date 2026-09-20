# 0004. Sympla price lots come from the event-bff tickets endpoint

Date: 2026-09-20. Status: accepted (supersedes the "deferred" outcome the spec
allowed for; nothing is deferred).

## Context

Spec §4: ticket prices are not in the event page HTML; a 30-minute recon was
budgeted to find the client-side request. Reading the page's JS chunks
(`assets-event-page.svc.sympla.com.br/evento/_next/static/chunks/*.js`) found
the ticket store's `getTickets()`:

```
GET https://event-page.svc.sympla.com.br/api/event-bff/purchase/event/<id>/tickets/grouped
```

Optional query parameters `invitationToken`, `promotionCode`, `affiliateCode`
are for private/discounted sales and are not used. The endpoint answers a plain
GET with no authentication, cookies or custom headers; verified with the
project's User-Agent on 2026-09-20 for paid, free and mixed events.

Response shape: `{"tickets": [...], "groups": [{"name", "tickets": [...],
"subgroups": [...]}]}`. Each ticket has `name`, `show`, `status`
(`STARTED`, `CLOSED`, ...), `isFree`, `salePriceMonetary{decimal,integer}`,
`netValueMonetary`, `feeMonetary`, `installments`, availability counters.

## Decision

- `fetch()` makes this request right after the event page, under the same
  per-source rate limit (two requests per event). No Playwright.
- `PriceLot.amount = salePriceMonetary.integer / 100` (what the buyer pays,
  service fee included), built from the integer cents so no float is involved.
  `netValueMonetary` was seen as `0` on a paid lot and is not used.
- Lots with `show == false` or `status == "CLOSED"` are skipped; tickets in
  `groups` and `subgroups` are walked recursively.
- If the request fails or the body is unparsable: the record is returned with
  empty `price_lots` and `raw["price_lots_pending"] = True` (paid events), or
  the single `Gratuito`/`0` lot (`paymentEventType == "free"`), and a
  `sympla.tickets_unavailable` warning is logged. The full response is not
  kept in `raw`; a re-crawl fetches it again.

## Consequences

- Paid events get real price lots in Phase 1; `price_lots_pending` only marks
  bileto entries and transient failures.
- This is an internal BFF, not a documented API; it can change or start
  requiring headers at any time. The adapter degrades to `price_lots_pending`
  rather than failing the event.
