# 0017. Nominatim clipped to the registry's metro areas, public instance behind it

Date: 2026-10-07. Status: superseded by ADR 0018 the same day (accepted
first; owner: "cull the nominatim from 50 GB to 10 GB or less"; fallback to
the public instance chosen over leaving outliers unresolved).

## Context

The self-hosted `nominatim` volume holds the Geofabrik Brazil extract,
`address` style, at about 50 GB. Freezing it (`FREEZE=true`, PR #70) drops the
osm2pgsql update tables, but the search tables of a whole country stay well
above the 10 GB the host and the Google Cloud disk (docs/DEPLOY.md §4) should
carry. `IMPORT_STYLE=street`/`admin` would shrink it further at the cost of
house numbers, which venues need (300 m dedupe, "events near me").

The pipeline geocodes venues of the 32 registry cities. Almost all of them lie
in those cities' metro areas, a few percent of Brazil's surface.

## Decision

- **Import a clipped PBF.** `ops/host/nominatim/clip.py` writes an `osmium
  extract` config with a 40 km square around each registry city, merging the
  squares that overlap (Greater Vitória is one area); `--strategy smart` keeps
  ways and relations on an edge whole. Compose takes it through the image's
  `PBF_PATH` from `ops/host/nominatim/pbf/` (gitignored, read-only mount).
  The full import stays one `.env` change away.
- **Fall back for what the clip misses.** `EVCRAWL_GEOCODE_FALLBACK_URL`
  (the public instance in `.env.example`) gets the whole query chain again
  when every local step answered nothing, in its own fetcher bucket at
  1 request/second whatever `EVCRAWL_GEOCODE_RPS` is (its usage policy,
  AGENTS.md §10). The cached response says `instance: "fallback"`. A failure
  there is `geocode.unavailable`, never a cached miss.
- **A registry city needs a centre.** `tests/test_clip.py` holds `CENTRES`
  equal to the registry's slugs.

## Consequences

- Disk: measured by the owner after the reimport (`ops/host/README.md`
  "Clipped"); `--radius-km` is the knob if it lands above 10 GB.
- A venue outside every area costs one local chain plus one public chain
  (up to 6 requests) the first time, then lives in `geocode_cache`. Local
  misses inside an area (bad addresses) also reach the public instance; at
  the observed miss rate that is a handful of requests per cycle.
- Many `instance = fallback` rows in one city mean its square is too small.
