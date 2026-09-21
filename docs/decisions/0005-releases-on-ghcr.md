# 0005. Release images are published to GHCR from git tags

Date: 2026-09-21. Status: accepted.

## Context

ADR 0001 makes the `runtime` image the release artifact and requires it to be
tagged with the git SHA plus a semver tag, but nothing published it anywhere:
CI only built it and threw it away. The VPS (Phase 1) has to pull it from a
registry the repository already trusts.

## Decision

A git tag `v<semver>` pushed on a commit that is on `main` triggers
`.github/workflows/release.yml`, which builds the `runtime` target and pushes it
to `ghcr.io/<repository-owner>/evcrawl` under two tags: the git tag and the
short git SHA. The registry is GitHub Container Registry because the repository
is already there, the workflow's own `GITHUB_TOKEN` can write packages (no
extra secret), and the package inherits the repository's private visibility.
Pre-releases use the semver suffixes `-dev.N` and `-rc.N`; a tag without a
suffix is a production release. Tags are annotated and signed by the person
cutting the release; agents prepare the commands but do not sign.

## Consequences

`latest` is never produced. A tag on a commit that is not on `main` fails the
workflow, so releases are always built from gated code. The VPS pulls by
explicit tag with a read-only `read:packages` token.
