# 0001. The Docker image is the release artifact

Date: 2026-09-20. Status: accepted (the `dev` target is covered by 0002).

## Context

The crawler, scheduler, CLI, and migrations must run on a Linux VPS next to
PostgreSQL+PostGIS and Redis. Nothing is deployed from a checkout.

## Decision

`docker build` of this repository produces the `evcrawl` image. Its entrypoint
is the `evcrawl` CLI and its default command is `scheduler`. Migrations ship
inside the image and are applied by running `evcrawl migrate` as a one-off
container, never on boot. The build is multi-stage on `python:3.12-slim`,
installs from the committed `uv.lock` with `--frozen`, and the runtime stage
runs as a non-root user with no build tools. Images are tagged with the git SHA
plus a semver tag on release; `latest` is never deployed.

## Consequences

Every change to dependencies, the Dockerfile, or the CLI entry point must be
verified with `docker build` and `docker run --rm evcrawl:dev --help`.
