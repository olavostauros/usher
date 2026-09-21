# 0002. One hosted `dev` container is the team workspace

Date: 2026-09-20. Status: accepted. Amended 2026-09-21: the owner's computer
is the host, with no planned move.

## Context

Several people work on the crawler from different machines and operating
systems. Local setups drift, and "works on my machine" costs review time. The
owner hosts the environment on their own computer, next to the release
container.

## Decision

The `Dockerfile` gains a `dev` target next to `runtime`. One long-running
`dev` container runs on the host (`docker compose up -d db redis dev`).
Members SSH into it (port 2222, key auth only, published on the host's
Tailscale address only). Each member has a Linux user, a home on the persistent
`homes` volume, and their own clone of the GitHub repository at `~/usher`.
Membership is data: `team/members.txt` plus `team/keys/<login>.pub`, applied by
`team/entrypoint.sh` at every container start. `db` and `redis` are shared.
Tools come only from the image; nothing is installed by hand in the container.
The host's own checkout is not mounted into the container.

## Consequences

- Availability is best-effort while the host is a personal computer; members
  push often. Volumes are backed up nightly (`ops/host/backup-volumes.sh`).
- Any change to `Dockerfile`, `pyproject.toml`, `uv.lock`,
  `docker-compose.yml`, or `team/` requires a rebuild by the host owner and
  drops open sessions; commits say "rebuild required".
- CI runs the gates inside the same `dev` image, so CI and the workspace
  cannot diverge.
- If the host ever changes machine, only `ops/host/` and the tailnet name
  change, not the image.
