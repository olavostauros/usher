# Security audits of the hosted dev container

`ops/audit/audit.py` scans the stack that AGENTS.md §4 describes: the `dev`
container (`usher-dev-1`), the `runtime` image, the shared `db` and `redis`
services, the compose wiring, `team/` (sshd, entrypoint), the repo's supply
chain and, with `--cve`, known vulnerabilities in the images and in `uv.lock`.
The `cerberus` agent (`.claude/agents/cerberus.md`) runs it, adds judgement,
and files a report here. A second agent fixes the report, one PR per finding.

Reports live in `reports/<date>-<target>.md`, newest last. The latest one is
the open queue; older ones are history.

## Running it

```bash
# host owner, from the repo root, container running
ops/audit/audit.py --container usher-dev-1 --cve --md docs/security/reports/$(date +%F)-usher-dev.md
# a member, inside the container (in-container checks only)
ops/audit/audit.py --inside
```

Read-only by construction. Standard library only. Exit 1 when a finding is at
or above `--fail-on` (default HIGH), so it can gate CI later.

## Severity rubric

| severity | meaning here |
|---|---|
| CRITICAL | unauthenticated remote access, or any member/agent reaching the host or root in the container |
| HIGH | one member/agent reaching another member's code, data or credentials; RCE in a sibling service (db, redis); a fixable CVE in a network-facing daemon |
| MEDIUM | a documented rule of AGENTS.md §4 that is not actually enforced; a denial-of-service path against the shared host; forwarding/exposure beyond the tailnet |
| LOW | hardening with no current exploit path (suid bits under no_new_privs, unpinned tags, missing CI scan); unfixed CVEs with no fix available |
| INFO | accepted by design, with the AGENTS.md section that accepts it; guardrails that are guidance, not boundaries |

## Fix contract (for the fixing agent)

Each finding in a report has: `id`, `severity`, `Component` (file and
directive, or container path), `Evidence` (a command and its output),
`Impact`, `Fix` (numbered steps naming files), `Verify` (a command and the
expected output), `Rebuild required`, `Status`.

The fixing agent:

1. Works from the newest report, highest severity first, in a branch named
   `fix/<finding-id>-<slug>` per finding (two findings that touch the same
   lines may share one branch; say so in both).
2. Follows AGENTS.md §11: small conventional commits, the commit body cites
   the finding id and the report path, `rebuild required` in the message when
   the report says so.
3. Runs the finding's `Verify` block after the change (against a rebuilt
   image when `Rebuild required: yes`: `docker compose build dev` on the
   host, or in CI) and pastes the output in the PR description. A fix without
   its verify output is not done.
4. Does not change the finding's severity or wording. If the fix is wrong for
   a reason the auditor missed, the PR says why and sets `Status: disputed`
   with one line; the auditor answers in the next report.
5. Sets `Status: fixed in #<pr>` in the report on the same branch, and
   `Status: accepted` only when the host owner said so in the PR.
6. Never touches the running container by hand (AGENTS.md §4: rebuild and
   restart is the host owner's move, announced to the team). Fixes are
   commits; the owner applies them.

The auditor re-runs after the owner rebuilds and files a new report; findings
that no longer reproduce are listed under "Resolved since last report".

## Scope notes

- The `dev` container is a shared machine on the owner's personal computer.
  "One member harms another" and "one member harms the host" are the two
  threat models that matter most; internet-facing exposure is out of scope
  while sshd is tailnet-only.
- Shared development credentials are accepted (AGENTS.md §4, "One shared
  database"); what they *grant* is not, hence findings about superuser and
  unauthenticated redis rather than about the credential itself.
- Trivy runs from the `aquasec/trivy:latest` image with the docker socket
  mounted into that scanner container only; the dev container never sees it.
- Grafana (phase 3 monitoring, `docs/specs/phase-3-monitoring.md`, landed
  2026-09-23) adds three things to the stack, all accepted by the owner's
  decision of 2026-09-22. A second published port, `3000`, on
  `${DEV_SSH_BIND}` under the same rule as sshd: tailnet address only, a
  missing variable binds `127.0.0.1`; `0.0.0.0:3000` in `ss -ltn` is a
  MEDIUM like any other exposure beyond the tailnet. Anonymous access as
  Grafana **Viewer**, no login, for anyone who reaches that port: they
  already reach sshd, and the dashboards show only what the views expose
  (counts, run metadata, venue names; never descriptions, raw snapshots or
  personal data). And `grafana_ro`, the db's first non-superuser role,
  `LOGIN` with `SELECT` on the `VIEWS` of `db/models.py` and nothing else;
  its password lives in the host `.env` (`GRAFANA_DB_PASSWORD`, so F-005
  covers it) and in the `grafana` service's environment, never exported to
  members. The audit verifies rather than re-decides: the bind address;
  that `grafana_ro` is denied on every table and cannot write
  (`tests/test_db_models.py` asserts it, `psql -U grafana_ro` from inside
  the container reproduces it); that sign-up, UI edits and plugins stay off
  (`docker-compose.yml`, `ops/grafana/provisioning/`); that the
  `grafana/grafana-oss` tag is pinned (F-002 applies to it as to the other
  images). A member with an editor or admin account, a plugin, or the
  datasource pointed at anything but the views would each be a finding.
