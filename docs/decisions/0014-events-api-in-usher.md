# 0014. The PWA's events API lives in this repository, read-only, on views

Date: 2026-09-24. Status: proposed (the scope PR for Phase 4;
`docs/specs/phase-4-api.md`). It becomes accepted when that PR merges.

## Context

AGENTS.md §1 said this repository "does not define the PWA's API layer". The
database is meant for the Usher PWA ("events near me"), but nothing serves
it: the only readers today are `ops/host/status.py` and Grafana, both on the
tailnet, both through the monitoring views as `grafana_ro`. The owner has
decided to build the API now, so the PWA can be developed against real data.

The other candidate home is `~/Work/ticket/server/`, the future backend of
the ticket-selling product. It does not exist yet, its concern is orders and
payments, and it would have to reach the crawl database across projects. The
crawl schema, the PostGIS knowledge and the test fixtures are all here.

## Decision

- The events API is a package of this repository (`src/evcrawl/api/`),
  shipped in the same `runtime` image and started by `evcrawl api` as its
  own compose service. There is one release, one git SHA, and one set of
  gates.
- It is **read-only by construction**. It connects as `api_ro`, a role with
  `SELECT` on the `api_*` views only (the `grafana_ro` pattern of migration
  `0005`). Sessions are read-only with a 3 s statement timeout. The API
  never holds `DATABASE_URL`.
- The `api_*` views and `API_VIEWS` in `db/models.py` are the contract
  between the pipeline and the API. The committed `docs/api/openapi.json` is
  the contract between the API and the PWA. Each has a test that fails on
  drift.
- It serves public event data only (§10) and takes filters, never
  identities. It stores nothing about PWA users, and it logs no IPs and no
  coordinates.
- Nothing about the API is public until the owner chooses how to expose it
  (spec §3 "Open"). Until then it is published on the Tailscale address only,
  like sshd and Grafana.

## Consequences

- AGENTS.md §1 scope changes to "defines the read-only events API the PWA
  consumes; nothing that writes". A write endpoint, user accounts or
  anything payment-related still belongs elsewhere.
- Renaming a column in an `api_*` view, or a field in the OpenAPI document,
  is a breaking change: it ships as `/v2` and keeps `/v1` until the PWA has
  moved.
- The API inherits the host's best-effort availability. Spec §3 "Open" lists
  what changes that.
- Two dependencies are added: `fastapi` and `uvicorn[standard]`.
