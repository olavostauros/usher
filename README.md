# usher

Code name for the **Evento Livre event crawler** (`evcrawl`). It crawls
Brazilian ticketing platforms and maintains a PostgreSQL + PostGIS database of
upcoming events. The build output of this repository is the `evcrawl` Docker
image; nothing is deployed from a checkout.

## Work on it (team workspace)

The team works inside one `dev` container hosted by the owner (AGENTS.md §4).
You need an SSH client and Tailscale; nothing else is installed locally.

```bash
ssh -p 2222 <login>@<host-tailnet-name>      # key is committed under team/keys/
git clone <repo> ~/usher && cd ~/usher       # first time only
uv sync && evcrawl --help
ruff check . && ruff format --check . && mypy && pytest -q
```

## Build and run the release image

```bash
docker build --target runtime -t evcrawl:dev --build-arg GIT_SHA="$(git rev-parse --short HEAD)" .
docker run --rm evcrawl:dev --help
docker compose up -d db redis
docker compose run --rm -e GRAFANA_DB_PASSWORD -e TEAM_DB_PASSWORD -e EVCRAWL_API_DB_PASSWORD evcrawl migrate
docker compose up -d evcrawl            # runs `evcrawl scheduler`
```

## Pull a published release

Pushing a `v<semver>` tag on a `main` commit publishes the `runtime` image to
GitHub Container Registry (private; `read:packages` token needed):

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-login> --password-stdin
docker pull ghcr.io/olavostauros/evcrawl:v0.1.0-dev.1   # or :<short-sha>
docker run --rm ghcr.io/olavostauros/evcrawl:v0.1.0-dev.1 version
```

There is no `latest` tag. Pre-releases carry a `-dev.N` / `-rc.N` suffix.

Host owners: see `ops/host/README.md` for bringing the workspace up.

## Layout

- `src/evcrawl/` — the package: `cli.py`, `config.py`, `scheduler.py`, `db/`,
  `sources/` (one adapter per platform: Sympla, uTicket, BlueTicket,
  TicketBolt, Zig), `pipeline/` (fetch, normalize, geocode, dedupe, upsert).
- `alembic/` — migrations, shipped inside the image.
- `tests/` — pytest; recorded HTTP fixtures under `tests/fixtures/http/`.
- `docs/decisions/` — short ADRs.
- `team/` — workspace membership (logins + SSH public keys).
- `ops/host/` — running the host (workspace and release, on the owner's
  computer); `ops/audit/` — read-only security scanner; `ops/backup.sh` —
  plain nightly `pg_dump` for running the image elsewhere.
