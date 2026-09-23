# Running the workspace host

The `dev` container from `docker-compose.yml` is the team's shared workspace
(AGENTS.md §4). This file is for whoever runs the host machine: the owner's
own computer, which also runs the `evcrawl` release container from the same
Compose project.

Host tailnet name: `<fill in after tailscale up>` (share it with members
privately, not in AGENTS.md).

## One-time host setup

```bash
# 1. Tailscale (Arch/Omarchy)
sudo pacman -S tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up            # opens a browser login; join the team tailnet
tailscale ip -4              # -> put this in .env as DEV_SSH_BIND

# 2. Environment
cd ~/Work/usher
cp .env.example .env
sed -i "s/^DEV_SSH_BIND=.*/DEV_SSH_BIND=$(tailscale ip -4)/" .env
sed -i "s/^NOMINATIM_PASSWORD=.*/NOMINATIM_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^GRAFANA_DB_PASSWORD=.*/GRAFANA_DB_PASSWORD=$(openssl rand -hex 16)/" .env

# 3. Your own key (once)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -C usher
cp ~/.ssh/id_ed25519.pub team/keys/<your-login>.pub   # <your-login> is in team/members.txt

# 4. Build and start the workspace (nominatim starts its import; see "Geocoder")
docker compose build dev
docker compose up -d db redis nominatim dev

# 5. Release the crawler: build the runtime image with the git SHA, migrate,
#    smoke-crawl every source with --limit, start the scheduler
ops/host/release.sh

# 6. Start on boot (user services keep running after logout)
loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp ops/host/usher.service ops/host/usher-backup.service ops/host/usher-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usher.service usher-backup.timer
```

Check: `ss -ltn | grep 2222` must show only the Tailscale IP (or 127.0.0.1),
never `0.0.0.0` or `*`.

## Is it crawling?

```bash
ops/host/status.py            # containers, totals, last run per source, verdict
ops/host/status.py --json     # the same for scripts
```

Read-only (`docker compose ps` and SELECTs), exit 1 when the scheduler is
down or a source is stale (no finished run in 6 h, AGENTS.md §9) or
erroring, 2 when it cannot probe. It is the one stack probe an agent on the
host may run; everything else through `docker` is the owner's. Members can
run it inside the container too (`psql` mode: no container states).

## Release the crawler (after a merge to main)

```bash
git pull
ops/host/release.sh           # build evcrawl:dev from HEAD, migrate, smoke, restart scheduler
```

The script refuses a dirty tree, stops at the first failing step, and does
not start the scheduler when the smoke crawl fails (`--no-start` skips that
step on purpose; `--city`, `--limit` tune the smoke). The `evcrawl` service
restarts with the Docker daemon once started (`restart: unless-stopped`);
`docker compose stop evcrawl` holds it down until the next release or
`docker compose up -d evcrawl`. Migrations are never applied on container
boot (AGENTS.md §9): this script is where they run.

The migrate step also applies `GRAFANA_DB_PASSWORD` from `.env` to the
read-only `grafana_ro` database role (migration 0005; Grafana's datasource
logs in with it). Set the variable before the release, or run the release
again after setting it; changing it later rotates the password the same way.
Without it the role exists but cannot log in, and the migrate step says so.

## Geocoder (self-hosted Nominatim)

The `nominatim` compose service (`mediagis/nominatim:5.3`) answers the
pipeline's geocoding (`NOMINATIM_URL=http://nominatim:8080` in `.env`) so the
33 cities of Phase 3 never lean on the public instance. It has no host port;
only containers on the compose network reach it. Its first start imports the
Geofabrik Brazil extract (`NOMINATIM_PBF_URL`, 1.9 GB download,
`IMPORT_STYLE=address`) and takes hours; every later start serves the data on
the `nominatim` volume (the image keeps an `import-finished` marker there).
Nothing waits for it: while the import runs, crawls log `geocode.unavailable`,
cache nothing, and retry the venue on their next run.

```bash
docker compose up -d nominatim                 # first start = the import
docker compose logs -f nominatim               # ... until "Starting Gunicorn"
docker compose ps nominatim                    # healthy once /status answers
docker compose exec nominatim curl -s localhost:8080/status   # -> OK
docker system df -v | grep usher_nominatim     # disk used by the volume
# smoke: venues resolve against the container (log lines name its URL)
docker compose run --rm evcrawl crawl zig --city vitoria --limit 5 \
  | grep -E 'geocode\.(lookup|unavailable)'
```

Measured on the owner's machine (fill in after the first import; the spec's
acceptance reads these numbers):

| import | disk (`usher_nominatim`) | wall-clock | threads / shm / RAM knobs |
|---|---|---|---|
| Brazil, `address` style, 2026-__-__ | __ GB | __ h | `NOMINATIM_THREADS=4`, `NOMINATIM_SHM_SIZE=1g`, 1GB / 4GB / 8GB |

Sizing knobs (`.env`, host owner only): `NOMINATIM_THREADS` (import and
indexing threads), `NOMINATIM_SHM_SIZE` (the image recommends half the RAM for
big imports; 1g is enough for one country), `NOMINATIM_SHARED_BUFFERS`,
`NOMINATIM_MAINTENANCE_WORK_MEM`, `NOMINATIM_EFFECTIVE_CACHE_SIZE` (the image
defaults of 2GB / 10GB / 24GB assume a dedicated server). Changing them after
the import only affects serving, not the data. `EVCRAWL_GEOCODE_RPS` in `.env`
is the pipeline's rate at the geocoder; it may go above 1 only here: the
pipeline clamps it to 1 against `nominatim.openstreetmap.org`.

Reimport (new extract, or a corrupted volume): `docker compose rm -sf
nominatim && docker volume rm usher_nominatim && docker compose up -d
nominatim`. Fallback to the public instance:
`NOMINATIM_URL=https://nominatim.openstreetmap.org` and
`EVCRAWL_GEOCODE_RPS=1.0` in `.env`, then `docker compose up -d dev evcrawl`
(members get the new value at their next login). The volume is **not**
backed up: it is reproducible from the PBF, and a restore is a reimport.

## Add or remove a member

1. Get their SSH public key (ed25519 preferred).
2. Save it as `team/keys/<login>.pub`, add `<login>` to `team/members.txt`
   (remove the line to revoke; keep or delete the key file).
3. Commit on a branch, PR, merge: `team: add <login>`.
4. On the host: `git pull && docker compose up -d --build dev`.
   Homes persist on the `homes` volume; a removed member's home stays but
   their key is deleted at boot, so they cannot log in.
5. Tell them: `ssh -p 2222 <login>@<host-tailnet-name>` and the joining steps
   in AGENTS.md §4. They must be on the tailnet.

## Rebuild after a recipe change

Any merged commit that says "rebuild required" (touches `Dockerfile`,
`pyproject.toml`, `uv.lock`, `docker-compose.yml`, `team/`):

```bash
git pull
docker compose up -d --build dev      # ~1 min; open SSH sessions are dropped
```

Announce it in the team channel first; members lose running tmux sessions.

## Run the DB-backed tests from the host

The `db` service publishes no host port, so `pytest` on the host skips the
database tests. Run the full suite in a throwaway `dev` container on the
compose network, against a scratch database that is dropped afterwards:

```bash
docker run --rm -v "$PWD:/work" -w /work --network usher_default \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e PGPASSWORD=evcrawl \
  -e EVCRAWL_TEST_DATABASE_URL=postgresql+psycopg://evcrawl:evcrawl@db:5432/host_scratch \
  --entrypoint bash evcrawl-dev:local -c \
  'createdb -h db -U evcrawl host_scratch; uv sync --frozen; \
   uv run pytest -q -p no:cacheprovider; dropdb -h db -U evcrawl host_scratch'
```

The container writes root-owned `__pycache__` into the mounted tree; `sudo
chown -R "$USER" .` before cleaning up. Members run the same tests inside the
container with their own `<login>_scratch` database (AGENTS.md §7).

## Backups and restore

`usher-backup.timer` runs `backup-volumes.sh` nightly at 03:30 and keeps 14
days under `ops/host/backups/` (gitignored). Only `pgdata` and `homes` are
snapshotted; `nominatim` is rebuilt from the PBF ("Geocoder" above). Restore
a volume:

```bash
docker compose stop dev db
docker run --rm -v usher_homes:/dst -v "$PWD/ops/host/backups":/src alpine \
  sh -c 'rm -rf /dst/* /dst/.[!.]* ; tar xzf /src/homes-YYYY-MM-DD.tgz -C /dst'
docker compose up -d db dev
```

## Limits and guardrails

The `dev` service runs with `no-new-privileges`, a minimal capability set, no
docker socket, `/tmp` on a 4 GB tmpfs, and caps from `.env`:
`DEV_MEMORY_LIMIT` (default 16g), `DEV_CPU_LIMIT` (default 12), 4096 pids.
Per-user process/file limits are in `team/limits.conf`. Raise them in `.env`
and `docker compose up -d dev` (no rebuild needed for `.env` changes).
Members and their agents cannot escalate, install packages, or touch each
other's homes; see AGENTS.md §4 "Agents in the workspace".

## Danger zone

- `docker compose down -v` deletes the database **and every member's home**,
  plus the Nominatim data (hours of reimport). Announce it, take a backup,
  wait for acknowledgements.
- Never change `DEV_SSH_BIND` to `0.0.0.0`. If Tailscale is down, the port is
  simply unreachable; that is the intended failure mode.
- Members have no host account and no sudo inside the container. Anything they
  need goes into the image via PR.
