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
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^TEAM_DB_PASSWORD=.*/TEAM_DB_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^NOMINATIM_PASSWORD=.*/NOMINATIM_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^GRAFANA_DB_PASSWORD=.*/GRAFANA_DB_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)/" .env

# 3. Your own key (once)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -C usher
cp ~/.ssh/id_ed25519.pub team/keys/<your-login>.pub   # <your-login> is in team/members.txt

# 4. Build and start the workspace (nominatim starts its import; see "Geocoder")
docker compose build dev
docker compose up -d db redis nominatim dev

# 5. Release the crawler: build the runtime image with the git SHA, migrate,
#    smoke-crawl every source with --limit, start the scheduler
ops/host/release.sh
docker compose up -d grafana   # dashboards on DEV_SSH_BIND:3000 (see "Dashboards")

# 6. Start on boot (user services keep running after logout)
loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp ops/host/usher.service ops/host/usher-backup.service ops/host/usher-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usher.service usher-backup.timer
```

Check: `ss -ltn | grep -E '2222|3000'` must show only the Tailscale IP (or
127.0.0.1), never `0.0.0.0` or `*`.

## Is it crawling?

```bash
ops/host/status.py                 # containers, scheduler, totals, one line per source
ops/host/status.py --city          # one line per source x city
ops/host/status.py --city sao-paulo
ops/host/status.py --json          # the same for scripts: sources, cities, scheduler
ops/host/status.py --hours 3       # a tighter freshness window than the views' 6 h
```

Read-only (`docker compose ps` and SELECTs on the monitoring views of
migration `0005`, the same views Grafana reads), exit 1 when the scheduler
is down or a source is stale (no finished run in 6 h, AGENTS.md §9) or
erroring in any city, 2 when it cannot probe. The scheduler's verdict comes
from its heartbeat: the `evcrawl` container upserts `scheduler_heartbeat`
every minute, and a beat older than 5 minutes is `down` even when the
container says `running` (a wedged scheduler; `release.sh` recreates it).
It is the one stack probe an agent on the host may run; everything else
through `docker` is the owner's. Members run it inside the container too
(`psql` mode: no container states, but the same scheduler line). The
default table rolls each source up over its cities (the worst city's
verdict wins) and names the failing cities in the `!!` lines; `--city`
shows the rows behind it.

## Dashboards (Grafana)

`http://<DEV_SSH_BIND>:3000` is the `grafana` compose service
(`grafana/grafana:13.0.9`, `docs/specs/phase-3-monitoring.md`): the
monitoring views of migrations `0005` and `0006`, read as the `grafana_ro`
role, the same views `status.py` reads, on four dashboards (Freshness, Crawl
runs, Catalog, Geocoder; `ops/grafana/README.md`). Anyone on the tailnet is
an anonymous **Viewer**, no
login; the `admin` user (password `GRAFANA_ADMIN_PASSWORD` in `.env`) is only
for the datasource's "Save & test" and never handed out. Sign-up and UI
edits are off: dashboards, the datasource and the provider are files under
`ops/grafana/` and change by PR; the service re-reads `ops/grafana/dashboards`
every 30 s, so `git pull` deploys a merged dashboard without a restart.

```bash
docker compose up -d grafana                # after both GRAFANA_* are in .env
ss -ltn | grep 3000                         # the Tailscale IP only, never 0.0.0.0
docker compose logs --tail 20 grafana       # "HTTP Server Listen" and no provisioning error
# datasource check: log in as admin at /connections/datasources/edit/evcrawl,
# "Save & test" -> "Database Connection OK"
```

The datasource logs in with `GRAFANA_DB_PASSWORD`, which `release.sh`'s
migrate step applies to the role ("Release the crawler" above): set both
variables, release once, then start Grafana. A password changed in `.env`
needs a release (the role) and `docker compose up -d grafana` (the
datasource) to match again. `GRAFANA_ADMIN_PASSWORD` is applied on the
volume's first start only; to change it later, `docker compose exec grafana
grafana cli admin reset-admin-password '<new>'` and update `.env` to match.
The `grafana` volume holds sessions and the admin user only and is not
backed up; `docker volume rm usher_grafana` after a `docker compose rm -sf
grafana` resets it with nothing lost.

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

Neither `evcrawl` nor `dev` reads `.env` wholesale (no `env_file`, F-026):
each gets an explicit `environment:` list in `docker-compose.yml`, and the
scheduler never holds `GRAFANA_DB_PASSWORD`, `TEAM_DB_PASSWORD`,
`GRAFANA_ADMIN_PASSWORD` or `NOMINATIM_PASSWORD`. The two role passwords
reach the migrate one-off alone, named on its command line; a migrate run
by hand must name them too, or it leaves both passwords as they are:

```bash
docker compose run --rm -e GRAFANA_DB_PASSWORD -e TEAM_DB_PASSWORD evcrawl migrate
```

A new setting the pipeline reads (`config.py`) is a new line in both
services' `environment:` lists; a variable only in `.env` does not reach
the containers.

## Database roles

The shared `db` has three login roles (F-014, F-001 in
`docs/security/reports/2026-09-24-usher-stack.md`):

| role | who | password in `.env` | may |
|---|---|---|---|
| `evcrawl` | the `evcrawl` release container (migrate, scheduler); `status.py` over the container's local socket | `POSTGRES_PASSWORD` | everything: the cluster superuser |
| `team` | every member and their agent, from the `dev` container (`DATABASE_URL`, `PG*`) | `TEAM_DB_PASSWORD` | `SELECT`/`INSERT`/`UPDATE`/`DELETE` on `public`, `CREATE SCHEMA <login>`, `createdb <login>_scratch`; no DDL or truncation in `public`, no server programs or files, no roles |
| `grafana_ro` | the Grafana datasource | `GRAFANA_DB_PASSWORD` | `SELECT` on the monitoring views |

`release.sh`'s migrate step creates `team`, applies `TEAM_DB_PASSWORD` and
re-grants it every table in `public` (`src/evcrawl/db/roles.py`), so a
password changed in `.env` needs a release, then `docker compose up -d dev`
for members' sessions to pick it up (open sessions keep the old variables
until they reconnect).

`POSTGRES_PASSWORD` initialises an empty `pgdata` volume only. To change it
on a live volume, including the first time after F-014 (the volume was
initialised with the old compose default):

```bash
sed -i '/^DATABASE_URL=/d' .env               # compose now builds each service's URL
# both set (setup step 2), letters and digits only; prints lengths, never values
awk -F= '/^(POSTGRES_PASSWORD|TEAM_DB_PASSWORD)=/{print $1, length($2)}' .env
docker compose up -d db                       # recreate db so its environment has the new value
# set it from db's own environment: never on screen, in argv or in the clipboard
docker compose exec -T db psql -U evcrawl -d evcrawl -v ON_ERROR_STOP=1 <<'SQL'
\getenv pw POSTGRES_PASSWORD
alter role evcrawl password :'pw';
SQL
# check over TCP, as the release connects (the local socket skips the password): prints 1
docker compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$(hostname -i | cut -d" " -f1)" -U evcrawl -d evcrawl -Atc "select 1"'
ops/host/release.sh                           # migrate as the superuser: creates and grants team
docker compose up -d --build dev              # members' DATABASE_URL/PG* become team's
```

Do not use `\password` with a pasted value: on 2026-09-24 a paste into its
hidden prompt set a password that did not match `.env` (both prompts agreed
on the same wrong text), and the migrate step failed with `password
authentication failed`. Between the `alter role` and the release, the running
scheduler cannot open new connections; run them back to back. The owner keeps
both passwords in the 1Password vault `usher` (`op://usher/POSTGRES_PASSWORD/password`,
`op://usher/TEAM_DB_PASSWORD/password`). Then check as a member, inside the
container:

```bash
psql -Atc "select rolsuper from pg_roles where rolname = current_user"   # f
psql -c "copy (select 1) to program 'id'"                                # ERROR: permission denied
```

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
compose network, against a scratch database that is dropped afterwards. It
connects as `team`, as a member would ("Database roles" above):

```bash
TEAM_PW=$(sed -n 's/^TEAM_DB_PASSWORD=//p' .env)
docker run --rm -v "$PWD:/work" -w /work --network usher_default \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e PGHOST=db -e PGUSER=team -e PGPASSWORD="$TEAM_PW" \
  -e EVCRAWL_TEST_DATABASE_URL="postgresql+psycopg://team:$TEAM_PW@db:5432/host_scratch" \
  --entrypoint bash evcrawl-dev:local -c \
  'createdb -T template_postgis host_scratch; uv sync --frozen; \
   uv run pytest -q -p no:cacheprovider; dropdb host_scratch'
```

The container writes root-owned `__pycache__` into the mounted tree; `sudo
chown -R "$USER" .` before cleaning up. Members run the same tests inside the
container with their own `<login>_scratch` database (AGENTS.md §7).

## Backups and restore

`usher-backup.timer` runs `backup-volumes.sh` nightly at 03:30 and keeps 14
days under `ops/host/backups/` (gitignored). Only `pgdata` and `homes` are
snapshotted; `nominatim` is rebuilt from the PBF ("Geocoder" above) and
`grafana` from the repository ("Dashboards" above). Restore a volume:

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

The other services have their own ceilings (F-024), because a member's SQL
runs in `db`, not in `dev`, and a Grafana panel, a Nominatim reimport or a
crawler bug in `evcrawl` could otherwise grow into the host's memory. All run
with `no-new-privileges` and `cap_drop: [ALL]`. `evcrawl` and `grafana`
(non-root images) add nothing back; `db`, `redis` and `nominatim` start as
root, chown their data and drop to their own user, so they keep `CHOWN`,
`DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID` (and `AUDIT_WRITE` for
`nominatim`, whose start script uses `sudo`). If one of them fails to start
after an image bump, `docker compose logs <service>` names the refused call.

| service | memory (`.env`, default) | pids |
|---|---|---|
| `db` | `DB_MEMORY_LIMIT`, 4g | 1024 |
| `redis` | `REDIS_MEMORY_LIMIT`, 512m | 128 |
| `nominatim` | `NOMINATIM_MEMORY_LIMIT`, 8g | 1024 |
| `grafana` | `GRAFANA_MEMORY_LIMIT`, 1g | 256 |
| `evcrawl` | `EVCRAWL_MEMORY_LIMIT`, 2g | 512 |

The cap is a cgroup limit: past it the kernel kills a process inside that
container only (in `db`, one backend; PostgreSQL restarts in recovery and the
others reconnect). `NOMINATIM_MEMORY_LIMIT` counts `/dev/shm`, so keep it
above `NOMINATIM_SHARED_BUFFERS` + `NOMINATIM_MAINTENANCE_WORK_MEM` +
`NOMINATIM_SHM_SIZE`; raise it for a reimport if the import is OOM-killed
(`docker compose logs nominatim`, `dmesg | grep -i oom`). Changing a limit is
a compose change, not a rebuild: edit `.env`, then `docker compose up -d
<service>` recreates it. Check what is applied:

```bash
docker inspect --format '{{.Name}} mem={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}} {{.HostConfig.SecurityOpt}} drop={{.HostConfig.CapDrop}}' \
  usher-db-1 usher-redis-1 usher-nominatim-1 usher-grafana-1 usher-evcrawl-1
docker stats --no-stream   # MEM USAGE / LIMIT per container
```

## Danger zone

- `docker compose down -v` deletes the database **and every member's home**,
  plus the Nominatim data (hours of reimport). Announce it, take a backup,
  wait for acknowledgements.
- Never change `DEV_SSH_BIND` to `0.0.0.0`. If Tailscale is down, the ports
  (sshd 2222, Grafana 3000) are simply unreachable; that is the intended
  failure mode.
- Members have no host account and no sudo inside the container. Anything they
  need goes into the image via PR.
