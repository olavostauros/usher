# 0015. The public events API goes out through a Cloudflare Tunnel from the host

Date: 2026-09-25. Status: proposed (Phase 4 task 5,
`docs/specs/phase-4-api.md` §3 "Open"). It becomes accepted when that PR
merges.

## Context

The Usher PWA is deployed at `https://eventolivre.com/usher/` and calls the
events API from the browser. Until now the API was published on the
Tailscale address only (ADR 0014, task 4). Spec §3 "Open" left three
questions to the owner: how the PWA reaches the API, which origins CORS
allows, and whether v1 needs a key. The `eventolivre.com` zone is already on
Cloudflare; the site itself is on GitHub Pages.

## Decision

- **Option (a), a tunnel from this host.** A named Cloudflare Tunnel,
  `usher-api`, run as the `cloudflared` compose service next to `api`,
  serves `https://api.eventolivre.com`. It is outbound-only: no port is
  published beyond `DEV_SSH_BIND`, TLS is Cloudflare's, and the one DNS
  record is the proxied CNAME that `cloudflared tunnel route dns` creates.
  The image is pinned by tag and digest like Grafana (F-022).
- **The PWA calls the API from the browser**, so the API sends CORS itself
  for exactly `https://eventolivre.com`: four fixed headers on every
  response, the 429 included, never `*`, no credentials
  (`src/evcrawl/api/cors.py`). Cloudflare passes them through; there is no
  CORS rule at the edge.
- **No API key in v1.** The data is public (AGENTS.md §10). Abuse control
  is the in-process rate limiter, keyed on the last `X-Forwarded-For`
  address (the one the tunnel appends) because every public request reaches
  the API from the tunnel's address.
- `/v1/docs` stays tailnet-only: the tunnel's ingress answers 404 there.

## Consequences

- The PWA goes down whenever the owner's computer does. Option (b), a
  hosted replica, is the move when the PWA has real users.
- The tunnel credentials are a new host secret (gitignored, mode 600); the
  account's `cert.pem` stays in the owner's `~/.cloudflared/`.
- A new public surface: `cerberus` re-audits it before announcement
  (spec task 5), and `status.py` reports the `cloudflared` container.
