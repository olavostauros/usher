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
sed -i "s/^GRAFANA_DB_PASSWORD=.*/GRAFANA_DB_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^EVCRAWL_API_DB_PASSWORD=.*/EVCRAWL_API_DB_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)/" .env

# 3. Your own key (once)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -C usher
cp ~/.ssh/id_ed25519.pub team/keys/<your-login>.pub   # <your-login> is in team/members.txt

# 4. Build and start the workspace
docker compose build dev
docker compose up -d db redis dev

# 5. Release the crawler: build the runtime image with the git SHA, migrate,
#    smoke-crawl every source with --limit, start the scheduler and the
#    events API (DEV_SSH_BIND:8000, see "Events API")
ops/host/release.sh
docker compose up -d grafana   # dashboards on DEV_SSH_BIND:3000 (see "Dashboards")

# 6. Start on boot (user services keep running after logout)
loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp ops/host/usher.service ops/host/usher-backup.service ops/host/usher-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usher.service usher-backup.timer
```

Check: `ss -ltn | grep -E '2222|3000|8000'` must show only the Tailscale IP
(or 127.0.0.1), never `0.0.0.0` or `*`.

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
is down, a container of the stack (the `api` service included) is not
running, or a source is stale (no finished run in 6 h, AGENTS.md §9) or
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
(`grafana/grafana:13.0.10`, `docs/SPECIFICATION.md` §6): the
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

## Events API

`http://<DEV_SSH_BIND>:8000` is the `api` compose service
(`docs/SPECIFICATION.md` §7, ADR 0014; the contract the PWA team reads is
`docs/api/README.md`): the `evcrawl:dev` image the scheduler runs, same
build and git SHA, started as `evcrawl api`. It logs in as the read-only
`api_ro` role ("Database roles" below: `SELECT` on the `api_*` views of
migration `0008` and nothing else, read-only sessions, a 3 s statement
timeout), never as the superuser: the service has no `env_file`, and its
`environment:` list is the `api_ro` URL, the CORS origins, the proxy flag
and `LOG_LEVEL`. Published on `DEV_SSH_BIND` only, like sshd and Grafana;
there is no authentication in v1 (the data is public, AGENTS.md §10). The
same service is public as `https://api.eventolivre.com` through the tunnel
of the next section, which forwards to it inside the compose network.
`release.sh` recreates it on the new image after the scheduler;
on the VM `ops/gcp/usher.service` starts it at boot. Since the cut-over
(2026-10-07) this host's unit does not start it; the commands below are
the VM's, over `gcloud compute ssh`, and are kept here as the service's
runbook.

```bash
docker compose up -d api                    # after a release applied EVCRAWL_API_DB_PASSWORD to api_ro
ss -ltn | grep 8000                         # the Tailscale IP only, never 0.0.0.0
docker compose ps api                       # healthy once /healthz answers 200 (about 15 s after start)
docker compose logs --tail 20 api           # one JSON line per request: route, status, ms; never IPs or coordinates
curl -s "http://$(tailscale ip -4):8000/healthz"   # {"ok":true,"git_sha":"<sha>","db":"ok"}
# what the container holds: EVCRAWL_API_* and LOG_LEVEL, no DATABASE_URL, no other password
docker inspect usher-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1
```

From a member's machine on the tailnet: `curl
http://<host-tailnet-name>:8000/healthz`, and the interactive docs at
`http://<host-tailnet-name>:8000/v1/docs`. That is the base URL the PWA
team develops against (`docs/api/README.md`, "Where it is").

The healthcheck asks `/healthz`, which answers 503 when `api_ro` cannot log
in, so `docker compose ps api` says `unhealthy` after a password mismatch:
`EVCRAWL_API_DB_PASSWORD` changed in `.env` needs a release (the role) and
`docker compose up -d api` (the URL) to match again, exactly like Grafana's.
`API_MEMORY_LIMIT` (default 512m) is its cap ("Limits and guardrails").
The rate limiter (60 requests a minute per client) lives in the process, so
`docker compose restart api` resets it; on the tailnet that is abuse
control, not a boundary.

## Public exposure (Cloudflare Tunnel)

`https://api.eventolivre.com` is the events API on the public internet
(spec task 5, ADR 0015): the named Cloudflare Tunnel `usher-api`, run by
the `cloudflared` compose service, which dials out to Cloudflare's edge and
forwards to `http://api:8000` inside the compose network. Nothing new
listens on the host; `DEV_SSH_BIND` stays the only published address. The
ingress rules are `ops/host/cloudflared/config.yml` (committed: `/v1/docs`
is a 404 in public, anything but `api.eventolivre.com` is a 404). The DNS
record is one proxied CNAME, `api` → the tunnel; no other record in the
zone belongs to this project, so never touch them (the site's GitHub Pages
and mail records live there too). CORS is the API's own
(`evcrawl.api.cors`, `EVCRAWL_API_CORS_ORIGINS`): never add a CORS rule at
Cloudflare.

Secrets, none committed, none printed: `~/.cloudflared/cert.pem` (the
account login, used only to create or route tunnels), the tunnel's
credentials `~/.cloudflared/<tunnel-id>.json`, and its copy the container
reads, `ops/host/cloudflared/credentials.json` (gitignored, mode 600,
owned by `CLOUDFLARED_USER`). Deleting the tunnel revokes them.

One-time setup (done 2026-09-25 up to the copy):

```bash
cloudflared tunnel login                              # once per machine, for the eventolivre.com zone
cloudflared tunnel create usher-api                   # writes ~/.cloudflared/<id>.json, prints <id>
cloudflared tunnel route dns usher-api api.eventolivre.com   # the proxied CNAME, nothing else
install -m 600 ~/.cloudflared/<id>.json ops/host/cloudflared/credentials.json
# .env: CLOUDFLARE_TUNNEL_ID=<id>, CLOUDFLARED_USER=$(id -u):$(id -g)
docker compose up -d cloudflared                      # on the VM only; its usher.service starts it at boot from then on
```

Check it:

```bash
cloudflared tunnel info usher-api                     # connectors listed = HEALTHY
docker compose ps cloudflared                         # healthy: `tunnel ready` against its metrics port
python3 ops/host/status.py                            # cloudflared=running (healthy)
curl -sSI -H 'Origin: https://eventolivre.com' https://api.eventolivre.com/v1/categories
                                                      # 200 and the four Access-Control-* headers
curl -sSI -H 'Origin: https://example.com' https://api.eventolivre.com/v1/categories
                                                      # 200 and no Access-Control-Allow-Origin
```

The API trusts the last `X-Forwarded-For` address for its rate limiter
(`EVCRAWL_API_TRUST_PROXY=1`, the compose default), which is the client
address the tunnel appends; `release.sh` warns when `.env` turns it off.
A release recreates `api` and leaves the tunnel alone; it reconnects by
service name. To take the API off the internet: `docker compose stop
cloudflared` (the CNAME then answers Cloudflare's 1033 error); to remove it
for good, `cloudflared tunnel route` has no delete, so delete the `api`
record in the Cloudflare dashboard and `cloudflared tunnel delete usher-api`.

## Release the crawler (after a merge to main)

```bash
git pull
ops/host/release.sh           # build evcrawl:dev from HEAD, migrate, smoke, restart scheduler and API
```

The script refuses a dirty tree, stops at the first failing step, and does
not start the scheduler or the API when the smoke crawl fails (`--no-start`
skips both on purpose; `--city`, `--limit` tune the smoke). The `evcrawl`
and `api` services restart with the Docker daemon once started
(`restart: unless-stopped`); `docker compose stop evcrawl` holds the
scheduler down until the next release or `docker compose up -d evcrawl`,
and the same for `api`. Migrations are never applied on container
boot (AGENTS.md §9): this script is where they run.

The migrate step also applies `GRAFANA_DB_PASSWORD` from `.env` to the
read-only `grafana_ro` database role (migration 0005; Grafana's datasource
logs in with it). Set the variable before the release, or run the release
again after setting it; changing it later rotates the password the same way.
Without it the role exists but cannot log in, and the migrate step says so.
`EVCRAWL_API_DB_PASSWORD` goes the same way to `api_ro`, the events API's
read-only role (migration 0008, `docs/SPECIFICATION.md` §7).

Neither `evcrawl` nor `dev` reads `.env` wholesale (no `env_file`, F-026):
each gets an explicit `environment:` list in `docker-compose.yml`, and the
scheduler never holds `GRAFANA_DB_PASSWORD`, `TEAM_DB_PASSWORD`,
`EVCRAWL_API_DB_PASSWORD` or `GRAFANA_ADMIN_PASSWORD`.
The three role passwords reach the migrate one-off alone, named on its
command line; a migrate run by hand must name them too, or it leaves those
passwords as they are:

```bash
docker compose run --rm -e GRAFANA_DB_PASSWORD -e TEAM_DB_PASSWORD -e EVCRAWL_API_DB_PASSWORD evcrawl migrate
```

A new setting the pipeline reads (`config.py`) is a new line in both
services' `environment:` lists; a variable only in `.env` does not reach
the containers.

## Database roles

The shared `db` has four login roles (F-014, F-001 in
`docs/security/reports/2026-09-24-usher-stack.md`):

| role | who | password in `.env` | may |
|---|---|---|---|
| `evcrawl` | the `evcrawl` release container (migrate, scheduler); `status.py` over the container's local socket | `POSTGRES_PASSWORD` | everything: the cluster superuser |
| `team` | every member and their agent, from the `dev` container (`DATABASE_URL`, `PG*`) | `TEAM_DB_PASSWORD` | `SELECT`/`INSERT`/`UPDATE`/`DELETE` on `public`, `CREATE SCHEMA <login>`, `createdb <login>_scratch`; no DDL or truncation in `public`, no server programs or files, no roles |
| `grafana_ro` | the Grafana datasource | `GRAFANA_DB_PASSWORD` | `SELECT` on the monitoring views |
| `api_ro` | the events API (the `api` compose service, "Events API" above) | `EVCRAWL_API_DB_PASSWORD` | `SELECT` on the `api_*` views; read-only sessions, 3 s statement timeout, 16 connections |

`release.sh`'s migrate step creates `team`, applies `TEAM_DB_PASSWORD` and
re-grants it every table in `public` (`src/evcrawl/db/roles.py`), so a
password changed in `.env` needs a release, then `docker compose up -d dev`
for members' sessions to pick it up (open sessions keep the old variables
until they reconnect). On the production VM `TEAM_ROLE_LOGIN=0` in `.env`
makes the same step keep `team` `NOLOGIN` (F-033; `ops/gcp/vm-setup.md` §4).

`POSTGRES_PASSWORD` initialises an empty `pgdata18` volume only. To change it
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

## PostgreSQL 18 (once, F-015)

Since F-015 (`docs/security/reports/2026-10-07-usher-stack.md`) the `db`
service runs `postgis/postgis:18-3.6` (PostgreSQL 18, PostGIS 3.6, Debian 13)
on a new volume, `pgdata18`, mounted at `/var/lib/postgresql` (the 18 image
keeps its cluster in `18/docker` under it). The 16-3.x images it replaces are
frozen on end-of-life Debian 11; `16-3.5` is still PostgreSQL 16.9. A new
major cannot open the old data directory, so each machine moves its data once
by dump and restore, with the old server still running for the dump. The old
volume `pgdata` is not touched: it is the way back.

**Done on the VM 2026-10-08** (writers stopped 11:22-11:31 UTC, counts
matched, docs/DEPLOY.md §9); **done on the host 2026-10-13** (counts
`22363|9930|22378|220` matched, migrations `0010`-`0011` applied by step 7),
see "If `db` already came up on the empty volume" below. Run it on the VM
first, then on the host, announced to the team (members'
sessions lose the db for a few minutes, and their `<login>_scratch` databases
are not carried over: `createdb -T template_postgis` again). Everything below
runs from the checkout (`~/usher` on the VM), **before** pulling the merge, so
`db` is still the 16 server. To read this section there first, `git fetch` and
`git show origin/main:ops/host/README.md`: a fetch leaves the working tree alone.

Two things are VM-only. **The snapshot**: `usher-daily` fires at 06:00 UTC,
the hour a crawl cycle starts, so it is taken mid-crawl; take a manual one
with the writers stopped (step 1b). **The boot unit**: `usher.service`
(`ops/gcp/usher.service`) runs `compose up` on every boot and `usher-restart`
restarts a preempted VM within minutes, so a preemption between the pull
(step 3) and the restore (step 5) would bring production up unattended on the
empty `pgdata18`. Disable the unit for that window (step 1c) and enable it
again only after the counts match (step 8).

```bash
# 1. stop the writers (VM; on the host `evcrawl` and `api` are already stopped)
#    between jobs: cycles start every 6 h at :00 UTC, the refresh runs hourly at
#    :30, and a job stopped halfway leaves its crawl_runs row "running". From here
#    until step 8 the public URL answers 502 (the tunnel is up, `api` is not)
docker compose stop evcrawl api
docker compose ps evcrawl api     # both "exited"; status.py calls the scheduler
                                  # "down" only 5 min after its last heartbeat
# 1b. VM only: a clean snapshot of the disk, once 1 shows both exited
#     (atlas from the host, or the owner; READY before you go on). Without
#     --storage-location gcloud stores it in the `us` multi-region
#   gcloud compute snapshots create usher-prod-pre-pg18 --project=usher-prod \
#     --source-disk=usher-prod --source-disk-zone=southamerica-east1-b \
#     --storage-location=southamerica-east1 --labels=usher=prod
# 1c. VM only: no unattended `compose up` until the restore is checked
sudo systemctl disable usher.service
# 2. dump with the running server and note the counts. One shell from here to the
#    end: umask holds only in the shell that set it, and the dump is the catalog
#    (docs/CYBERSECURITY.md §1, F-025), so it is owner-only. backups/ is gitignored and
#    does not exist on a fresh checkout (the VM's)
umask 077
mkdir -p ops/host/backups
docker compose exec -T db pg_dump -U evcrawl -d evcrawl --format=custom \
  > ops/host/backups/evcrawl-pg16.dump
ls -lh ops/host/backups/evcrawl-pg16.dump    # -rw------- and not empty (VM: 263 MB)
docker compose exec -T db psql -U evcrawl -d evcrawl -Atc "select
  (select count(*) from events), (select count(*) from venues),
  (select count(*) from source_listings), (select count(*) from geocode_cache)"
# 3. the new image on its new volume: initdb makes `evcrawl` from POSTGRES_PASSWORD
#    and PostGIS in evcrawl and template_postgis
docker compose stop db
git pull --ff-only
docker compose pull db
docker compose up -d db
docker compose exec -T db psql -U evcrawl -Atc 'show server_version'   # 18.6 or later
# 4. the three roles, NOLOGIN, with the flags of src/evcrawl/db/roles.py: the dump
#    carries their grants and the owners of members' <login> schemas, not passwords.
#    Pasted, the closing SQL must start its line (an indented one never ends the heredoc)
docker compose exec -T db psql -U evcrawl -d evcrawl -v ON_ERROR_STOP=1 <<'SQL'
create role grafana_ro nologin nosuperuser nocreatedb nocreaterole noinherit;
create role api_ro nologin nosuperuser nocreatedb nocreaterole noinherit;
create role team nologin nosuperuser nocreaterole createdb noreplication nobypassrls;
SQL
# 5. restore, without --exit-on-error: the dump carries the tiger/topology schemas
#    the image already made, and only those "already exists" errors may show
#    (docs/DEPLOY.md step 7 met exactly 3)
docker compose exec -T db pg_restore -U evcrawl -d evcrawl \
  < ops/host/backups/evcrawl-pg16.dump 2>&1 | grep 'error:'
# 6. the counts of step 2 again; they must match
# 7. passwords, bounds and grants: the migrate one-off of AGENTS.md §5 (a no-op
#    upgrade; it prints grafana_ro and api_ro "exists, bounded, login set",
#    team "exists, login set")
docker compose run --rm -e GRAFANA_DB_PASSWORD -e TEAM_DB_PASSWORD \
  -e EVCRAWL_API_DB_PASSWORD evcrawl migrate
docker compose exec -T db psql -U evcrawl -d evcrawl -Atc 'select postgis_full_version()'  # POSTGIS="3.6...
# 8. writers back, the reader first. VM: `sudo systemctl enable usher.service`,
#    `docker compose up -d api`, then (502 until it is healthy, ~15 s)
#    curl -s https://api.eventolivre.com/healthz   -> "db":"ok"
#    then `docker compose up -d evcrawl` and `python3 ops/host/status.py`
#    (scheduler up, heartbeat fresh); then Grafana's pull + up if it is behind
#    host: `docker compose up -d grafana dev`, and a member runs ops/db/team-role-checks.sh
```

**If `db` already came up on the empty volume.** A `compose up` after the
pull and before step 2 starts the 18 server on an empty `pgdata18`: `status.py`
fails on a missing view and `public` holds only `spatial_ref_sys`. Nothing is
lost; `pgdata` still holds the 16 cluster. That is how the host was found on
2026-10-13. Step 2 then dumps from a one-off 16 server on the old volume, and
steps 4-8 run as written (step 7 is a real upgrade if the old data was behind
`main`, so `docker compose build evcrawl` first):

```bash
umask 077; mkdir -p ops/host/backups
docker run -d --name usher-pg16 --network none \
  -v usher_pgdata:/var/lib/postgresql/data postgis/postgis:16-3.4
docker exec usher-pg16 pg_isready -U evcrawl          # accepting connections
docker exec usher-pg16 pg_dump -U evcrawl -d evcrawl --format=custom \
  > ops/host/backups/evcrawl-pg16.dump
# the counts of step 2 with `docker exec usher-pg16 psql ...`, then
docker stop usher-pg16 && docker rm usher-pg16
```

After seven quiet days the owner deletes the dump and the old volume
(`docker volume rm usher_pgdata`). Until then the way back is `docker compose
stop db`, `git checkout <the commit before the merge> -- docker-compose.yml`,
`docker compose up -d db`: the 16 server opens `pgdata` as it was at step 2,
and whatever was written since is lost (the scheduler re-crawls it). On the
VM the snapshot of step 1b is the last resort: a new disk from it holds the
16 cluster and the checkout as they were before the pull.

## Geocoder

Usher runs no geocoder of its own (ADR 0018). Most venues arrive with the
source's coordinates and never reach it; the rest are looked up once at the
public Nominatim (`NOMINATIM_URL=https://nominatim.openstreetmap.org`,
1 request/second, the project User-Agent) and kept in `geocode_cache`, so a
venue costs at most three requests the first time it is seen. If the public
instance is down or refuses us, crawls log `geocode.unavailable`, cache
nothing and retry the venue next run; the Geocoder dashboard and
`venues without location` in `status.py` show the backlog.

Retiring the old self-hosted service on a host that still has it (once, after
the merge; the volume holds tens of GB and nothing reads it any more):

```bash
cd ~/Work/usher && git pull
# .env: NOMINATIM_URL=https://nominatim.openstreetmap.org, and delete the
#       EVCRAWL_GEOCODE_FALLBACK_URL, NOMINATIM_PASSWORD and other NOMINATIM_* lines
docker compose rm -sf nominatim
docker volume rm usher_nominatim
rm -rf ops/host/nominatim                       # the clipped PBF, if one was made
docker compose up -d dev evcrawl                # pick up the new NOMINATIM_URL
cp ops/host/usher.service ~/.config/systemd/user/ && systemctl --user daemon-reload
```

## Production on Google Cloud

Production (everything above except the `dev` workspace and this host's
`db`) is moving to one Spot VM, `usher-prod`, following `docs/DEPLOY.md`: the
cloud steps are the `atlas` agent's, everything on the VM is yours by hand
(`ops/gcp/vm-setup.md`, then steps 7-9 of `docs/DEPLOY.md` §6). The VM runs the
same compose file, `ops/gcp/usher.service` instead of this directory's
unit, and `release.sh` and `status.py` unchanged over `gcloud compute ssh
usher-prod --tunnel-through-iap`. Only one scheduler crawls at a time and
only one host should hold the tunnel for long: the cut-over order is step 9.
This host keeps `db` as the team's development database, refreshed from a
production dump when you say so. This section grows into the full runbook
at step 11; until then `docs/DEPLOY.md` is the source.

**Since the cut-over (2026-10-07, docs/DEPLOY.md step 9) this host never runs
`evcrawl`, `api` or `cloudflared` again.** Its `usher.service` starts only
`db`, `redis`, `dev` and `grafana` (security finding F-030: a second
scheduler would crawl against the stale development db and a second
tunnel connector would serve `api.eventolivre.com` from it). `release.sh`
and the "Events API" and "Public exposure" sections above are the VM's
procedure now, run there over `gcloud compute ssh usher-prod
--tunnel-through-iap`. Never `docker compose up -d evcrawl`, `api` or
`cloudflared` here; if you need the host to serve again, stop the VM's
first (one scheduler, one connector). After pulling this change:

```bash
cp ops/host/usher.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user cat usher.service | grep -E '^Exec'   # neither line names evcrawl, api or cloudflared
```

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
days under `ops/host/backups/` (gitignored). Only `pgdata18` and `homes` are
snapshotted (a `pgdata-*.tgz` from before "PostgreSQL 18" above is a 16 data
directory that only the `postgis/postgis:16-3.4` image opens); `grafana` is rebuilt from the repository ("Dashboards" above).

The tarballs hold the whole db and every home, so they are the owner's alone
(F-025): the script keeps `backups/` at mode 700 and writes each file 600.
Files from before that change are root-owned and world-readable; once, on
the host:

```bash
sudo chmod 700 ops/host/backups
sudo chmod 600 ops/host/backups/*.tgz
stat -c '%a %n' ops/host/backups ops/host/backups/*   # 700 for the directory, 600 for every file
```

Restore a volume:

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
runs in `db`, not in `dev`, and a Grafana panel or a
crawler bug in `evcrawl` could otherwise grow into the host's memory. All run
with `no-new-privileges` and `cap_drop: [ALL]`. `evcrawl`, `api` and
`grafana` (non-root images) add nothing back; `db` and `redis` start as
root, chown their data and drop to their own user, so they keep `CHOWN`,
`DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`. If one of them fails to start
after an image bump, `docker compose logs <service>` names the refused call.

| service | memory (`.env`, default) | pids |
|---|---|---|
| `db` | `DB_MEMORY_LIMIT`, 4g | 1024 |
| `redis` | `REDIS_MEMORY_LIMIT`, 512m | 128 |
| `grafana` | `GRAFANA_MEMORY_LIMIT`, 1g | 256 |
| `evcrawl` | `EVCRAWL_MEMORY_LIMIT`, 2g | 512 |
| `api` | `API_MEMORY_LIMIT`, 512m | 256 |

The cap is a cgroup limit: past it the kernel kills a process inside that
container only (in `db`, one backend; PostgreSQL restarts in recovery and the
others reconnect; `dmesg | grep -i oom` names it). Changing a limit is
a compose change, not a rebuild: edit `.env`, then `docker compose up -d
<service>` recreates it. Check what is applied:

```bash
docker inspect --format '{{.Name}} mem={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}} {{.HostConfig.SecurityOpt}} drop={{.HostConfig.CapDrop}}' \
  usher-db-1 usher-redis-1 usher-grafana-1 usher-evcrawl-1 usher-api-1
docker stats --no-stream   # MEM USAGE / LIMIT per container
```

## Danger zone

- `docker compose down -v` deletes the database **and every member's home**,
  Announce it, take a backup,
  wait for acknowledgements.
- Never change `DEV_SSH_BIND` to `0.0.0.0`. If Tailscale is down, the ports
  (sshd 2222, Grafana 3000, the API 8000) are simply unreachable; that is
  the intended failure mode.
- Members have no host account and no sudo inside the container. Anything they
  need goes into the image via PR.
