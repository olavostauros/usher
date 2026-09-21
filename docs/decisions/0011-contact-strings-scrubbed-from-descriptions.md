# 0011. Contact strings are scrubbed from descriptions at normalize time

Date: 2026-09-21. Status: accepted.

## Context

The Phase 2 close scan (spec `phase-2-sources.md` §6 item 4) found one event
whose public description text ends with an organizer's `contato@<domain>`
address. No adapter collects contact fields, and every adapter strips them
from `raw` before the snapshot is stored, but free text written by the
organizer can carry an e-mail address or a phone number anyway. AGENTS.md
§10 says no organizer e-mail in the database, fixtures or logs, and asks for
LGPD minimisation of whatever personal data shows up in raw snapshots. A
personal address (a Gmail account, a mobile number) can appear in a
description exactly the same way as a corporate one.

## Decision

`normalize()` scrubs `description` before anything else uses it: e-mail
addresses and Brazilian phone numbers with an area code are replaced by the
placeholder `[contato removido]` (`scrub_contacts` in `pipeline/normalize.py`).
The scrub runs before `content_hash`, so an organizer editing only the contact
string does not create a new `event_versions` row. Titles, venues and
organizer names are not scrubbed (they are names, not free text; the
organizer contact fields are already dropped at parse time). `raw_snapshots`
stay the faithful record of what the source published; they hold no contact
fields and are not shown to users.

Phone matching requires an area code (`(27) 99999-8888`, `27 3333-4444`,
`+55 27 9 9999 8888`, `5527999998888`) and is guarded against longer digit
runs, so an 8-digit CEP, a date, a price or an order number is left alone.

## Consequences

- Events already stored get the scrubbed description on their next crawl,
  as one ordinary `event_versions` row (the hash changes once).
- A description that is nothing but a contact string becomes the placeholder.
- Rejected: scrubbing the snapshots too (they are the evidence of what was
  published, compressed JSON, and already free of contact fields), and
  accepting the finding as public business data (the rule does not
  distinguish corporate from personal addresses, and a regex over the
  description is cheap).
