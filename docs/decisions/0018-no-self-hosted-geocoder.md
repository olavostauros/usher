# 0018. No self-hosted geocoder: the public Nominatim, cached

Date: 2026-10-07. Status: accepted (owner, 2026-10-07: "we're removing this
nominatim from usher operation. it's too big and the operation doesn't need
it"). Supersedes ADR 0017 and the self-hosted geocoder of
`docs/specs/phase-3-national.md` §3.

## Context

The compose `nominatim` service held the Brazil extract at ~50 GB (ADR 0017
set out to clip it to 10 GB), needed up to 8 GB of RAM on the host and 3 GB of
the 8 GB production VM (docs/DEPLOY.md §4), and an import of hours on every
refresh. Meanwhile the geocoder has little left to do: on 2026-10-07 the
catalogue had 9,930 venues and 22 without a location. Most sources give
coordinates, which skip the geocoder, and every address that was looked up is
already in `geocode_cache`, which lives in `db` and travels with its dumps.

## Decision

- **Remove the service.** No `nominatim` compose service or volume, no clip
  tooling (`ops/host/nominatim/`), no `NOMINATIM_*` host settings, no
  `EVCRAWL_GEOCODE_FALLBACK_URL`. `usher.service` (host and Google Cloud) and
  `status.py` stop naming it.
- **Keep the geocoder, pointed at the public instance.** `NOMINATIM_URL` is
  `https://nominatim.openstreetmap.org` (already the code's default). The
  pipeline is unchanged otherwise: source coordinates first, then the cache,
  then the address → street → venue query chain at 1 request/second (the
  clamp of `rate_limit_for`, the instance's usage policy, AGENTS.md §10),
  with the project User-Agent. A miss is cached for 30 days; a failure is
  `geocode.unavailable` and retried next run.

## Consequences

- Tens of GB of disk and up to 8 GB of RAM back on the host; the production
  VM's limits sum to ~4.8 GB.
- New venues without source coordinates cost up to three public requests
  each, once. If the public instance blocks or throttles us, those venues
  stay without a location (out of radius queries)
  until it answers again; `v_geocode_health` and the Geocoder dashboard show
  the backlog. Bringing a geocoder back is a new ADR.
- The existing `geocode_cache` rows, including those from the self-hosted
  instance, stay valid (`provider = 'nominatim'` either way).
