---
name: add-source-adapter
description: Add or change a ticketing-platform source adapter (AGENTS.md §6) — recon, sanitized HTTP fixtures, discover()/fetch() implementation, parsing tests, registry and scheduler wiring. Use for any new source (Sympla, uTicket, BlueTicket, TicketBolt, Zig) or a change to how one is crawled.
---

# Add or change a source adapter

Follow AGENTS.md §6 in this order. Never write to the database from an adapter.

## 1. Recon (read-only, before any code)

1. `curl -sL -A "$EVCRAWL_USER_AGENT" https://<source>/robots.txt` — stop and ask the owner if crawling
   is forbidden (AGENTS.md §10).
2. Fetch one listing page and one event page with the same `-A` header. Look, in this order, for:
   a public JSON API → embedded JSON (`__NEXT_DATA__`, `self.__next_f.push` flight chunks, JSON-LD) →
   HTML → headless browser (ask first).
   For a JavaScript SPA, first open the listing in a browser with the network tab filtered to
   XHR/fetch (or run a throwaway Playwright script that logs `page.on("response")` URLs) and note
   the JSON endpoint the page calls. The adapter then calls that endpoint through `fetch_text`;
   the browser is a recon tool only, never part of the adapter without the owner's approval.
3. Note pagination (`?page=N`, `total`, `limit`), the canonical event URL pattern, the id, timezone
   handling, and where prices live. Write the findings into `docs/specs/` (or an ADR) with the date.

## 2. Fixtures (AGENTS.md §6 step 6)

```bash
mkdir -p tests/fixtures/http/<source>
curl -sL -A "$EVCRAWL_USER_AGENT" "<listing url>" -o tests/fixtures/http/<source>/listing-<city>-p1.html
curl -sL -A "$EVCRAWL_USER_AGENT" "<event url>"   -o tests/fixtures/http/<source>/event-<id>.html
python3 .claude/skills/add-source-adapter/sanitize_fixture.py tests/fixtures/http/<source>/*.html
grep -c '@' tests/fixtures/http/<source>/*.html   # must be 0 outside redacted@example.org
```

The sanitizer replaces every e-mail address with `redacted@example.org`, removes `<script src=...>`
tags and inline analytics snippets, and keeps the data-bearing scripts (`__NEXT_DATA__`, `__next_f`,
JSON-LD). Also record an empty/last page for pagination tests. Keep fixtures under ~1 MB each.

For an API-first source, record the JSON responses instead of (or next to) the HTML:

```bash
curl -sL -A "$EVCRAWL_USER_AGENT" -H "Accept: application/json" "<search url>" \
  -o tests/fixtures/http/<source>/search-<city>-p1.json
curl -sL -A "$EVCRAWL_USER_AGENT" -H "Accept: application/json" "<search url, past the last page>" \
  -o tests/fixtures/http/<source>/search-<city>-empty.json
python3 .claude/skills/add-source-adapter/sanitize_fixture.py tests/fixtures/http/<source>/*.json
```

## 3. Implement

- `src/evcrawl/sources/<name>.py`: subclass `SourceAdapter` (`src/evcrawl/sources/base.py`).
  - `name`, `rate_limit = RateLimit(requests_per_second=1.0, max_retries=5)` (changing it: ask first).
  - `discover(city)` yields canonical event URLs (or ids) for the region, paginating until a page
    yields nothing.
  - `fetch(id_or_url)` returns one `EventRecord`. Free events get one `PriceLot(amount=0)`, never an
    empty list. Times are UTC-aware `datetime`s with the IANA zone in `timezone`.
  - HTTP goes through `evcrawl.pipeline.fetch` (shared client, rate limit, retries, User-Agent from
    settings). Do not create your own `httpx` client in the adapter.
  - Never copy attendee/buyer data or organizer e-mails into `EventRecord` (AGENTS.md §10).
- `tests/test_sources_<name>.py`: parse the fixtures offline; assert ids, titles, dates, venue,
  coordinates, category, pagination end. No network in tests.
- Register in `src/evcrawl/sources/__init__.py`, then add the standing job in
  `src/evcrawl/scheduler.py` (`build_scheduler`).

## 4. Verify

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
uv run evcrawl crawl <name> --city <city> --dry-run
uv run evcrawl crawl <name> --city <city> --limit 5
```

Commit style: `feat(<name>): discover listing pages`, `feat(<name>): parse event page`, one branch per task, PR into `main`.
