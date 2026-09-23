# Grafana (monitoring dashboards)

Files the `grafana` compose service mounts read-only
(`docs/specs/phase-3-monitoring.md`):

- `provisioning/datasources/evcrawl.yml`: the one datasource, the shared `db`
  as the read-only `grafana_ro` role (migration 0005; password from
  `GRAFANA_DB_PASSWORD` in the host `.env`).
- `provisioning/dashboards/evcrawl.yml`: the file provider that loads every
  `*.json` under `dashboards/`, UI edits disabled, re-read every 30 s.
- `dashboards/*.json`: the four dashboards (spec task 4), uid
  `evcrawl-<file>`, UTC, refresh every minute, linked to each other through
  the `evcrawl` tag:
  - `freshness.json`: scheduler heartbeat age and release, verdict counts,
    the city x source grid coloured by verdict (a `groupingToMatrix`
    transform on `v_source_city_freshness`), age vs SLA bars, the "not
    fresh" table; variables `source` and `city`.
  - `crawl-runs.json`: runs, errors, mean duration and skipped ratio per
    hour and source, throughput, created/merged/removed and versions/reviews
    per day (`v_crawl_runs_hourly`), the last error line per source x city
    (`v_source_city_last_run`); variables `source` and `kind`.
  - `catalog.json`: totals, active events by UF and by category, the
    per-city table, listings per source, the review queue, removed per day
    (`v_catalog_by_city`, `v_catalog_by_category`, `v_listings_by_source`).
  - `geocoder.json`: located-venue coverage, venues without location, cache
    hit ratio and sizes, unresolved venues by city and by name
    (`v_geocode_health`, `v_catalog_by_city`, `v_venues_unresolved`).

  Every panel and variable queries a monitoring view (`VIEWS` in
  `src/evcrawl/db/models.py`; migration `0006` added the three the Catalog
  and Geocoder panels needed) and nothing else: `grafana_ro` cannot see a
  table. `tests/test_grafana_queries.py` loads each file, expands the
  Grafana macros and variables it uses (`$__timeFilter(col)`,
  `${var:sqlstring}`; a new variable needs a sample value in the test's
  `SAMPLES`), runs every query through `EXPLAIN` and then executes it as
  `grafana_ro` on the scratch database, and checks that a `time_series`
  target returns exactly `time, metric, value`. What the test cannot do is
  render: after a change, open the dashboard on the host and look.

Dashboards change by PR: edit the JSON, open a PR, and the host picks it up
on the next `git pull` (no restart). The `grafana` volume holds only what
Grafana keeps for itself (sessions, the admin user) and is not backed up;
everything that matters is in this directory. Running it and the URL are in
`ops/host/README.md`, "Dashboards".
