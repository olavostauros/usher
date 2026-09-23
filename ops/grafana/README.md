# Grafana (monitoring dashboards)

Files the `grafana` compose service mounts read-only
(`docs/specs/phase-3-monitoring.md`):

- `provisioning/datasources/evcrawl.yml`: the one datasource, the shared `db`
  as the read-only `grafana_ro` role (migration 0005; password from
  `GRAFANA_DB_PASSWORD` in the host `.env`).
- `provisioning/dashboards/evcrawl.yml`: the file provider that loads every
  `*.json` under `dashboards/`, UI edits disabled, re-read every 30 s.
- `dashboards/*.json`: the dashboards (spec task 4). Every panel queries a
  monitoring view (`VIEWS` in `src/evcrawl/db/models.py`) and nothing else;
  `tests/test_grafana_queries.py` runs each panel's SQL through `EXPLAIN`.

Dashboards change by PR: edit the JSON, open a PR, and the host picks it up
on the next `git pull` (no restart). The `grafana` volume holds only what
Grafana keeps for itself (sessions, the admin user) and is not backed up;
everything that matters is in this directory. Running it and the URL are in
`ops/host/README.md`, "Dashboards".
