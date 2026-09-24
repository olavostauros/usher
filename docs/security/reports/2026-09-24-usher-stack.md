# Security audit: the usher stack (dev, db, redis, nominatim, grafana, evcrawl)

- Date: 2026-09-24 19:07Z (scan); triage 2026-09-24
- Mode: host, the owner's session (tool: ops/audit/audit.py `--container usher-dev-1 --cve`, plus read-only HTTP probes of Grafana, `ss`, `tailscale status`, `gh api`)
- Dev image: evcrawl-dev:local `sha256:7ed745ab9421`; runtime `evcrawl:dev`, build e9d7c0f (ops/host/status.py)
- Repository commit: 02fa21a (branch docs/cybersecurity, PR #45; the running stack is e9d7c0f, which differs only in CYBERSECURITY.md)
- Previous report: docs/security/reports/2026-09-20-usher-dev.md
- Findings: high 5, medium 9, low 9, info 5 (28 open; 1 resolved since the last report)

Fix contract: see docs/security/README.md. One PR per finding unless two touch the same file; the commit message cites the finding id; set `Status` in this file when merged.

Findings carried over from 2026-09-20 keep their old id, so `fix/F-0xx-*` branches still match. The scanner numbers findings afresh on every run, and its ids for this run exist only in its JSON. New findings start at F-021.

## Summary

| id | severity | title | component | rebuild | since | status |
|---|---|---|---|---|---|---|
| F-008 | HIGH | Shared uv cache is world-writable: cross-member code execution | Dockerfile (dev stage): `chmod -R a+rwX /opt/uv-cache`; team/entrypoint.sh UV_CACHE_DIR | yes | 2026-09-20 | fixed in #49 |
| F-014 | HIGH | Every member connects to Postgres as a superuser | docker-compose.yml (service db: POSTGRES_USER=evcrawl) | yes | 2026-09-20 | fixed in #50 |
| F-015 | HIGH | PostgreSQL 16.4 is behind the 16.x security releases | docker-compose.yml (image: postgis/postgis:16-3.4) | no | 2026-09-20 | open |
| F-019 | HIGH | Fixable HIGH/CRITICAL CVEs in image postgis/postgis:16-3.4 | image postgis/postgis:16-3.4 (Dockerfile / docker-compose.yml) | yes | 2026-09-20 | open |
| F-022 | HIGH | Grafana 13.0.2 is seven patch releases behind, missing Grafana security fixes and a patched Go runtime | docker-compose.yml (service grafana: `image: grafana/grafana-oss:13.0.2`) | no | new | fixed in #48 |
| F-001 | MEDIUM | Default/hardcoded Postgres superuser password in compose | docker-compose.yml (service db) | no | 2026-09-20 | fixed in #50 |
| F-010 | MEDIUM | sshd allows unrestricted TCP forwarding (no PermitOpen) | team/sshd.conf | yes | 2026-09-20 | open |
| F-011 | MEDIUM | Members can add their own SSH keys (authorized_keys is member-owned) | team/sshd.conf (AuthorizedKeysFile) | yes | 2026-09-20 | open |
| F-012 | MEDIUM | No size limit on the homes volume: a member can fill the host's root filesystem | docker-compose.yml (volumes: homes) | no | 2026-09-20 | open |
| F-013 | MEDIUM | Redis accepts unauthenticated admin commands from every member | docker-compose.yml (service redis) | no | 2026-09-20 | open |
| F-016 | MEDIUM | Fixable HIGH/CRITICAL CVEs in image evcrawl-dev:local | image evcrawl-dev:local (Dockerfile / docker-compose.yml) | yes | 2026-09-20 | open |
| F-023 | MEDIUM | Anonymous Grafana viewers can run arbitrary SQL as grafana_ro through /api/ds/query, with no statement timeout | docker-compose.yml (service grafana: `GF_AUTH_ANONYMOUS_ENABLED` | no | new | fixed in #52 |
| F-024 | MEDIUM | Only dev is hardened: db, redis, nominatim, grafana and evcrawl have no memory/pid caps, default capabilities and no no-new-privileges | docker-compose.yml (services db | no | new | fixed in #53 |
| F-027 | MEDIUM | `main` has no branch protection: review and CI gates are convention only | GitHub repository settings (branch `main` | no | new | open |
| F-002 | LOW | Base images and CI actions pinned by mutable tag, not digest | Dockerfile | yes | 2026-09-20 | open |
| F-004 | LOW | No image vulnerability scan in CI | .github/workflows/ci.yml | no | 2026-09-20 | open |
| F-007 | LOW | setuid/setgid binaries present in the dev image | Dockerfile (dev stage) | yes | 2026-09-20 | open |
| F-017 | LOW | Unfixed HIGH/CRITICAL CVEs in image evcrawl-dev:local (no upstream fix yet) | image evcrawl-dev:local | no | 2026-09-20 | open |
| F-018 | LOW | Unfixed HIGH/CRITICAL CVEs in image evcrawl:dev (no upstream fix yet) | image evcrawl:dev | no | 2026-09-20 | open |
| F-020 | LOW | Unfixed HIGH/CRITICAL CVEs in image postgis/postgis:16-3.4 (no upstream fix yet) | image postgis/postgis:16-3.4 | no | 2026-09-20 | open |
| F-021 | LOW | Nominatim image CVEs: every HIGH/CRITICAL is in linux-libc-dev (kernel headers) | docker-compose.yml (service nominatim: `image: mediagis/nominatim:5.3`) | no | new | open |
| F-025 | LOW | Host backups of pgdata and homes are world-readable | ops/host/backup-volumes.sh (`mkdir -p "$BACKUP_DIR"`; `alpine:3.20 tar czf ...`) | no | new | open |
| F-026 | LOW | `env_file: .env` gives the evcrawl and dev containers host secrets they do not use | docker-compose.yml (services dev and evcrawl: `env_file: .env`) | no | new | fixed in #54 |
| F-003 | INFO | Workflow does not restrict GITHUB_TOKEN permissions at the top level (repository default is read) | .github/workflows/ci.yml | no | 2026-09-20 | open |
| F-006 | INFO | Agent guard hook is bypassable by quoting (guidance, not a boundary) | .claude/hooks/guard.py | no | 2026-09-20 | open |
| F-009 | INFO | Shared database credentials are world-readable inside the container (accepted by design) | team/entrypoint.sh (/etc/environment | no | 2026-09-20 | open |
| F-028 | INFO | A write collaborator is not in team/members.txt | GitHub repository collaborators; team/members.txt | no | new | open |
| F-029 | INFO | Forking is allowed on the private repository | GitHub repository settings (`allow_forking`) | no | new | open |

## Re-check of the 2026-09-20 findings

| id | 2026-09-20 | 2026-09-24 | state | note |
|---|---|---|---|---|
| F-001 | MEDIUM | MEDIUM | still open | `POSTGRES_PASSWORD: evcrawl` unchanged in docker-compose.yml |
| F-002 | LOW | LOW | still open, wider | two more tag-pinned images (nominatim, grafana), plus alpine in the backup script and trivy in the scanner; Actions allow any action |
| F-003 | LOW | INFO | mitigated, not fixed | repository default token is `read` (verified); the scanner passes only on a substring |
| F-004 | LOW | LOW | still open | no scan job in CI |
| F-005 | LOW | - | **resolved** | `stat -c %a .env` -> 600 |
| F-006 | INFO | INFO | still open | the guard hook still passes `bash -c "..."` |
| F-007 | LOW | LOW | still open | same 13 setuid/setgid binaries in the rebuilt dev image |
| F-008 | HIGH | HIGH | still open | /opt/uv-cache still `drwxrwxrwx` after the rebuild |
| F-009 | INFO | INFO | still open (accepted) | AGENTS.md §4 "One shared database" |
| F-010 | MEDIUM | MEDIUM | still open | `permitopen any`; now on the tailnet address (DEV_SSH_BIND is set) |
| F-011 | MEDIUM | MEDIUM | still open | authorized_keys still member-owned |
| F-012 | MEDIUM | MEDIUM | still open | /home on the host's root btrfs, 26% used (was 23%) |
| F-013 | MEDIUM | MEDIUM | still open | `ACL WHOAMI` -> default; `CONFIG GET dir` answers |
| F-014 | HIGH | HIGH | still open | members still `evcrawl super=true`; `grafana_ro` is the only other login role |
| F-015 | HIGH | HIGH | still open | server still 16.4 (also confirmed by `version()`) |
| F-016 | MEDIUM | MEDIUM | still open | quinn-proto in uv 0.11.19 |
| F-017 | LOW | LOW | still open | same unfixed Debian 13.7 set |
| F-018 | LOW | LOW | still open | same |
| F-019 | HIGH | HIGH | still open | postgis/postgis:16-3.4 unchanged |
| F-020 | LOW | LOW | still open | same |

No finding regressed. None of F-001 to F-020 has had a fix PR: `git log 5d60b02..HEAD -- Dockerfile team/ docker-compose.yml .github/` shows only feature work (nominatim, grafana, the CI db service, automerge).

## Resolved since last report

- **F-005** (host `.env` readable by other host users): `stat -c '%a %U %n' .env` -> `600 olavostauros /home/olavostauros/Work/usher/.env`. Fixed by hand on the host, and the scanner no longer reports it. There is no `ops/host/.env`.

## Findings

### F-008 [HIGH] Shared uv cache is world-writable: cross-member code execution

- Category: lateral-movement
- Component: Dockerfile (dev stage): `chmod -R a+rwX /opt/uv-cache`; team/entrypoint.sh UV_CACHE_DIR
- Rebuild required: yes
- Status: fixed in #49
- Since: 2026-09-20

Evidence:

```
drwxrwxrwx root /opt/uv-cache (and every subdirectory / .http metadata file under wheels-v6, sdists-v9, interpreter-v4)
```

Impact: uv verifies hashes when it downloads an archive, not when it reuses an already-unpacked wheel from the cache. Any member (or their agent) can edit `/opt/uv-cache/wheels-v6/pypi/<pkg>/...` and the next `uv sync` of every other member links that code into their .venv and runs it under their user. This also contradicts AGENTS.md §4 ('/opt ... read-only for members').

Fix:

1. Dockerfile dev stage: keep the pre-warmed cache but make it read-only: replace `chmod -R a+rwX /opt/uv-cache` with `chmod -R a+rX,go-w /opt/uv-cache`.
2. team/entrypoint.sh: export `UV_CACHE_DIR=$HOME/.cache/uv` (drop /opt/uv-cache from ENV_VARS; write the per-user value into /etc/profile.d/evcrawl.sh as `export UV_CACHE_DIR="$HOME/.cache/uv"`).
3. Seed on first login: in the same profile.d snippet, `[ -d "$HOME/.cache/uv" ] || cp -r /opt/uv-cache "$HOME/.cache/uv" 2>/dev/null` so first `uv sync` stays fast.
4. Mention in AGENTS.md §4 that caches are per user.

Verify:

```
stat -c %a /opt/uv-cache -> 755; as a member: `test -w /opt/uv-cache; echo $?` -> 1; `uv cache dir` -> /home/<login>/.cache/uv
```

References: https://docs.astral.sh/uv/concepts/cache/

### F-014 [HIGH] Every member connects to Postgres as a superuser

- Category: privilege-escalation
- Component: docker-compose.yml (service db: POSTGRES_USER=evcrawl), team/entrypoint.sh (PGUSER/DATABASE_URL)
- Rebuild required: yes
- Status: fixed in #50
- Since: 2026-09-20

Evidence:

```
evcrawl super=true createrole=true createdb=true
16.4 (Debian 16.4-1.pgdg110+2)
off
```

Impact: The `evcrawl` role the whole team shares is the cluster superuser. Any member or agent can: run OS commands inside the db container (`COPY ... TO PROGRAM`), read and write files there, read `pg_authid`, create roles, and DROP/TRUNCATE anything regardless of the client-side guard hook (which only inspects shell commands). The db container is on the owner's personal computer. AGENTS.md §4's database discipline is therefore unenforced.

Fix:

1. Keep `evcrawl` (superuser) for `docker compose run --rm evcrawl migrate` only; move its password to the host `.env`.
2. Add an init script `ops/db/init/10-team-role.sql` mounted at `/docker-entrypoint-initdb.d/` (runs on first init; for the existing volume run it once by hand): `CREATE ROLE team LOGIN PASSWORD '...' NOSUPERUSER NOCREATEROLE NOCREATEDB; GRANT CONNECT ON DATABASE evcrawl TO team; GRANT USAGE, CREATE ON SCHEMA public TO team; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO team; GRANT CREATE ON DATABASE evcrawl TO team;` (CREATE on the database lets members make their `<login>` schema, per §4).
3. Grant on existing tables once: `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO team; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO team;`.
4. team/entrypoint.sh: PGUSER/PGPASSWORD/DATABASE_URL default to the `team` role.
5. Members needing a scratch database (§7) get `CREATEDB` on `team`, not superuser.

Verify:

```
as a member: psql -Atc "select rolsuper from pg_roles where rolname=current_user" -> f; psql -c "COPY (select 1) TO PROGRAM 'id'" -> ERROR: permission denied
```

### F-015 [HIGH] PostgreSQL 16.4 is behind the 16.x security releases

- Category: known-vulnerabilities
- Component: docker-compose.yml (image: postgis/postgis:16-3.4)
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
server_version 16.4; image postgis/postgis:16-3.4 created 2024-10-14, base Debian 11 (EOSL per trivy)
live, 2026-09-24 (`select version()` as grafana_ro, see F-023): PostgreSQL 16.4 (Debian 16.4-1.pgdg110+2); extensions: postgis 3.4.3, postgis_topology 3.4.3, fuzzystrmatch 1.2, postgis_tiger_geocoder 3.4.3
```

Impact: 16.4 predates fixes for CVE-2024-10976/10977/10978/10979 (16.5), CVE-2025-1094 (psql SQL injection, 16.7), CVE-2025-4207 (16.9) and CVE-2025-8714/8715 (16.10) among others; the image's OS layer is end-of-life Debian 11 with 27 CRITICAL unfixed CVEs and PostGIS 3.4.3 has CVE-2026-73515. Reachable by every member (and by forwarded laptops). Unchanged since 2026-09-20. It matters a little more now: `grafana_ro` gives anonymous Grafana viewers a SQL session on this server (F-023), so server bugs reachable by a low-privilege role no longer need an SSH key.

Fix:

Bump `image: postgis/postgis:16-3.5` (exists; same PG major so the data directory is reused, no dump/restore) and pin its digest. After the first start run `ALTER EXTENSION postgis UPDATE;` (or `SELECT postgis_extensions_upgrade();`) once. Add the image to the trivy job. Announce as a rebuild + restart of `db` (AGENTS.md §4). Consider `postgis/postgis:17-3.5` as a separate, planned major upgrade.

Verify:

```
psql -Atc 'show server_version' -> 16.10 or later; psql -Atc 'select postgis_full_version()'
```

References: https://www.postgresql.org/support/security/, https://hub.docker.com/r/postgis/postgis/tags

### F-019 [HIGH] Fixable HIGH/CRITICAL CVEs in image postgis/postgis:16-3.4

- Category: known-vulnerabilities
- Component: image postgis/postgis:16-3.4 (Dockerfile / docker-compose.yml)
- Rebuild required: yes
- Status: open
- Since: 2026-09-20

Evidence:

```
postgis/postgis:16-3.4: OS debian 11.11 (END OF LIFE); CRITICAL=27; fixable packages=85; packages with no fix yet=46
dirmngr 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
e2fsprogs 1.46.2-2 -> 1.46.2-2+deb11u1: CVE-2022-1304(H)
gnupg 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gnupg-l10n 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gnupg-utils 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpg 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpg-agent 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpg-wks-client 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpg-wks-server 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpgconf 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpgsm 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
gpgv 2.2.27-2+deb11u2 -> 2.2.27-2+deb11u3: CVE-2025-68973(H)
libcom-err2 1.46.2-2 -> 1.46.2-2+deb11u1: CVE-2022-1304(H)
libde265-0 1.0.11-0+deb11u3 -> 1.0.11-0+deb11u4: CVE-2026-33164(H)
libext2fs2 1.46.2-2 -> 1.46.2-2+deb11u1: CVE-2022-1304(H)
```

Impact: Each has an upstream fix available; these are the ones a rebuild or image bump actually removes.

Fix:

Bump the base image / tool version that carries the package (for `uv` bundled crates: bump the uv tag in both COPY --from lines and .mise.toml; for OS packages: `apt-get upgrade` is not the answer, rebuild from a newer base tag). Re-run this audit with --cve.

Verify:

```
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 postgis/postgis:16-3.4
```

### F-022 [HIGH] Grafana 13.0.2 is seven patch releases behind, missing Grafana security fixes and a patched Go runtime

- Category: known-vulnerabilities
- Component: docker-compose.yml (service grafana: `image: grafana/grafana-oss:13.0.2`)
- Rebuild required: no
- Status: fixed in #48
- Since: new

Evidence:

```
$ curl -s http://$DEV_SSH_BIND:3000/api/health
{ "database": "ok", "version": "13.0.2", "commit": "3fcdbc5a" }
$ gh api repos/grafana/grafana/releases --jq '.[0:3][] | "\(.tag_name) \(.published_at)"'
v13.2.2 2026-09-15T12:21:32Z
v13.1.6 2026-09-15T12:25:01Z
v13.0.9 2026-09-15T12:43:46Z
$ for t in v13.0.7 v13.0.8 v13.0.9; do gh api repos/grafana/grafana/releases/tags/$t --jq .body | grep -A3 '### Security'; done
v13.0.7: CVE-2026-17183
v13.0.8: CVE-2026-12704, CVE-2026-14199, CVE-2026-19475
v13.0.9: CVE-2026-15815, CVE-2026-76154, CVE-2026-79656
trivy, this run: grafana/grafana-oss:13.0.2: OS alpine 3.23.4; CRITICAL=1; fixable packages=40; packages with no fix yet=0
target usr/share/grafana/bin/grafana: 23 HIGH + 1 CRITICAL, all fixable; Go stdlib 1.26.3 -> 1.26.6:
  CVE-2026-56853 net/http unencrypted HTTP/2 DoS, CVE-2026-56862 crypto/tls KeyUpdate DoS,
  CVE-2026-56860 net/url quadratic DoS, CVE-2026-56858 html/template XSS, CVE-2026-42504 mime DoS;
  GHSA-r277-6w6q-xmqw (CRITICAL) kin-openapi v0.133.0 -> 0.144.0, ValidationHandler fail-open auth
scanner's per-package list (alpine curl and c-ares, bundled plugins, Go libraries):
c-ares 1.34.6-r0 -> 1.34.8-r0: CVE-2026-33630(H)
curl 8.19.0-r0 -> 8.20.0-r0: CVE-2026-5773(H), CVE-2026-6276(H)
curl 8.19.0-r0 -> 8.22.0-r0: CVE-2026-11352(H), CVE-2026-11586(H), CVE-2026-12064(H), CVE-2026-8286(H), CVE-2026-8458(H) ...
github.com/apache/thrift v0.23.1-0.20260429145742-d2acd3c49e58 -> 0.24.0: CVE-2026-43871(H)
github.com/getkin/kin-openapi v0.133.0 -> 0.141.0: CVE-2026-76905(H)
github.com/getkin/kin-openapi v0.133.0 -> 0.142.0: CVE-2026-77354(H)
github.com/getkin/kin-openapi v0.133.0 -> 0.144.0: GHSA-r277-6w6q-xmqw(C)
github.com/grafana/tempo v1.5.1-0.20251027222923-cbe5f845dc7b -> 2.10.3: CVE-2026-28377(H)
github.com/grafana/tempo v1.5.1-0.20251027222923-cbe5f845dc7b -> 2.8.4, 2.9.2, 2.10.2: CVE-2026-21728(H)
github.com/prometheus/prometheus v0.305.3 -> 0.311.3: CVE-2026-42151(H)
golang.org/x/crypto v0.49.0 -> 0.52.0: CVE-2026-39828(H), CVE-2026-39829(H), CVE-2026-39830(H), CVE-2026-39831(H), CVE-2026-39832(H) ...
golang.org/x/crypto v0.49.0 -> 0.55.0: CVE-2026-56854(H)
golang.org/x/crypto v0.52.0 -> 0.55.0: CVE-2026-56854(H)
golang.org/x/net v0.52.0 -> 0.53.0: CVE-2026-33814(H)
golang.org/x/net v0.52.0 -> 0.55.0: CVE-2026-25681(H), CVE-2026-27136(H), CVE-2026-39821(H)
```

Impact: Grafana is the only daemon in the stack that answers without authentication (anonymous Viewer, docs/security/README.md scope note), and it holds the `grafana_ro` password and the admin session store. Grafana shipped seven security fixes after 13.0.2. The server binary's Go runtime also has HTTP/2, TLS and URL-parsing DoS bugs that any client on the tailnet can trigger. The release notes do not describe the seven Grafana CVEs, so I could not rule out that one is reachable by an anonymous Viewer. That keeps this HIGH under the rubric ("a fixable CVE in a network-facing daemon"). I did not verify whether Grafana uses kin-openapi's `ValidationHandler` on a request path. The curl and c-ares items run only in the healthcheck, and the bundled-plugin items only for datasources that are not provisioned. They are not the reason for the severity.

Fix:

1. docker-compose.yml, service grafana: `image: grafana/grafana-oss:13.0.9@sha256:<digest>`. It is the same minor, so the provisioning format is unchanged; the digest pin is F-002 for this image.
2. Host owner: `docker compose pull grafana && docker compose up -d grafana`. Then `docker compose logs --tail 20 grafana` shows "HTTP Server Listen" and no provisioning error, and the four dashboards render.
3. Re-run this audit with `--cve`. Some bundled-plugin Go stdlib items may remain until Grafana rebuilds them; list what remains in the PR.

Verify:

```
curl -s http://$DEV_SSH_BIND:3000/api/health   -> "version": "13.0.9"
trivy image --severity HIGH,CRITICAL --ignore-unfixed grafana/grafana-oss:13.0.9   -> no entry under usr/share/grafana/bin/grafana
```

References: https://github.com/grafana/grafana/releases/tag/v13.0.9

### F-001 [MEDIUM] Default/hardcoded Postgres superuser password in compose

- Category: weak-credentials
- Component: docker-compose.yml (service db)
- Rebuild required: no
- Status: fixed in #50
- Since: 2026-09-20

Evidence:

```
POSTGRES_PASSWORD: evcrawl
```

Impact: The superuser password is public in git; combined with SSH port-forwarding any member can reach the db from their laptop as superuser.

Fix:

Move POSTGRES_PASSWORD to the host-side `.env` (`POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env}`) and give members a non-superuser role (see the postgres finding).

Verify:

```
grep -n POSTGRES_PASSWORD docker-compose.yml  -> a ${...} reference
```

### F-010 [MEDIUM] sshd allows unrestricted TCP forwarding (no PermitOpen)

- Category: lateral-movement
- Component: team/sshd.conf
- Rebuild required: yes
- Status: open
- Since: 2026-09-20

Evidence:

```
sshd -T: allowtcpforwarding yes; permitopen any; permitlisten any
```

Impact: `ssh -L` from any member's laptop reaches anything the container can reach: db:5432 (as superuser today), redis:6379 (no auth today), the docker bridge gateway, and the internet through the host's egress. It also lets laptop-side tools bypass the in-container guard hook entirely.

Fix:

Add to team/sshd.conf: `PermitOpen db:5432 redis:6379` (keeps editor DB tooling working) or `AllowTcpForwarding no` if that is not needed; add `PermitListen none`. Consider `AllowAgentForwarding no` unless members rely on it for git (root in the container could otherwise use their forwarded agent).

Verify:

```
sshd -T | grep -E 'permitopen|permitlisten|allowtcpforwarding'
```

### F-011 [MEDIUM] Members can add their own SSH keys (authorized_keys is member-owned)

- Category: persistence
- Component: team/sshd.conf (AuthorizedKeysFile), team/entrypoint.sh (install ... $home/.ssh/authorized_keys)
- Rebuild required: yes
- Status: open
- Since: 2026-09-20

Evidence:

```
sshd -T: authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2; ~olavostauros/.ssh/authorized_keys owned by olavostauros, mode 0600, writable
```

Impact: AGENTS.md §4 promises 'an agent cannot add a user or a key' and that membership is a commit under team/. In fact any member, or a compromised coding agent running as them, can append a second key and keep access after their key is rotated or removed from team/keys (the entrypoint only deletes the file for logins missing from members.txt; it never rewrites it for active ones).

Fix:

1. team/sshd.conf: `AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u`.
2. team/entrypoint.sh: `install -d -m 0755 -o root -g root /etc/ssh/authorized_keys.d` once, then per member `install -m 0644 -o root -g root "$keyfile" /etc/ssh/authorized_keys.d/$login`; delete `/etc/ssh/authorized_keys.d/*` for logins not active. Stop writing `$home/.ssh/authorized_keys`.
3. Keep `StrictModes yes` (default).

Verify:

```
sshd -T | grep authorizedkeysfile -> /etc/ssh/authorized_keys.d/%u; as a member: `echo x >> ~/.ssh/authorized_keys` then a new ssh with a fresh key is refused
```

### F-012 [MEDIUM] No size limit on the homes volume: a member can fill the host's root filesystem

- Category: denial-of-service
- Component: docker-compose.yml (volumes: homes), ops/host/README.md
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
/dev/mapper/root  1636826112 410280680 1224846200      26% /home
tmpfs                4194304       692    4193612       1% /tmp
/dev/mapper/root /home btrfs rw,relatime,compress=zstd:3,ssd,space_cache=v2,subvolid=279,subvol=/@ 0 0
```

Impact: `/home` is a plain directory on the host's root filesystem (/var/lib/docker/volumes/usher_homes). One dataset download, a runaway agent loop, or a fork of a big repo can fill the disk of the owner's personal computer, taking down the host, the db and every other member. `/tmp` is capped at 4g; `/home` is not. AGENTS.md §4 only asks members to keep homes small.

Fix:

Pick one on the host: (a) btrfs: `btrfs subvolume create /var/lib/docker/volumes/usher_homes/_data` before first `up`, then `btrfs quota enable` + `btrfs qgroup limit 64G` on it; (b) filesystem-agnostic: a fixed-size image `truncate -s 64G homes.img; mkfs.ext4 homes.img` mounted as a loop device and declared as an external volume with `driver_opts: {type: none, o: bind, device: /mnt/usher-homes}`. Document in ops/host/README.md; add a `df` line to the nightly backup script's log.

Verify:

```
inside: `df -P /home` shows the capped size; host: `btrfs qgroup show` or `losetup -a`
```

### F-013 [MEDIUM] Redis accepts unauthenticated admin commands from every member

- Category: lateral-movement
- Component: docker-compose.yml (service redis)
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
default
dir
redis_version:7.4.11
```

Impact: Any member or agent can `FLUSHALL` the shared queue, `CONFIG SET dir/dbfilename` + `SAVE` to write files inside the redis container (the classic redis-to-shell primitive; the alpine image runs redis as a non-root user, so it is a foothold, not a host escape), and `DEBUG`/`MODULE` if enabled. With unrestricted SSH forwarding this is reachable from laptops too.

Fix:

1. Host `.env`: `REDIS_PASSWORD=<random>`.
2. compose redis service: `command: ["redis-server", "--requirepass", "${REDIS_PASSWORD:?}", "--rename-command", "CONFIG", "", "--rename-command", "DEBUG", "", "--rename-command", "FLUSHALL", ""]` (or an ACL file with a `team` user `-@dangerous +@all`).
3. `REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0` in the dev and evcrawl services; healthcheck becomes `redis-cli -a "$REDIS_PASSWORD" ping`.

Verify:

```
redis-cli -u "$REDIS_URL" CONFIG GET dir -> (error) ERR unknown command / NOPERM; PING -> PONG
```

References: https://redis.io/docs/latest/operate/oss_and_stack/management/security/

### F-016 [MEDIUM] Fixable HIGH/CRITICAL CVEs in image evcrawl-dev:local

- Category: known-vulnerabilities
- Component: image evcrawl-dev:local (Dockerfile / docker-compose.yml)
- Rebuild required: yes
- Status: open
- Since: 2026-09-20

Evidence:

```
evcrawl-dev:local: OS debian 13.7; CRITICAL=3; fixable packages=1; packages with no fix yet=33
quinn-proto 0.11.14 -> 0.11.15: GHSA-4w2j-m93h-cj5j(H)
```

Impact: Each has an upstream fix available; these are the ones a rebuild or image bump actually removes. Unchanged: the dev image was rebuilt (now `sha256:7ed745ab9421`) but the uv pin is still 0.11.19.

Fix:

Bump the base image / tool version that carries the package (for `uv` bundled crates: bump the uv tag in both COPY --from lines and .mise.toml; for OS packages: `apt-get upgrade` is not the answer, rebuild from a newer base tag). Re-run this audit with --cve.

Verify:

```
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 evcrawl-dev:local
```

### F-023 [MEDIUM] Anonymous Grafana viewers can run arbitrary SQL as grafana_ro through /api/ds/query, with no statement timeout

- Category: network-exposure
- Component: docker-compose.yml (service grafana: `GF_AUTH_ANONYMOUS_ENABLED`, `GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer`); src/evcrawl/db/roles.py `ensure_grafana_role` and alembic/versions/0005_monitoring_views.py (no role settings on grafana_ro)
- Rebuild required: no
- Status: fixed in #52
- Since: new

Evidence:

```
# from the host, no cookie, no login
$ curl -s http://$DEV_SSH_BIND:3000/api/access-control/user/permissions | jq -r 'keys[]' | grep datasources
datasources.id:read
datasources:query
datasources:read
$ curl -s http://$DEV_SSH_BIND:3000/api/frontend/settings | jq -c '{anonymousEnabled,viewersCanEdit,exploreEnabled,snapshotEnabled,publicDashboardsEnabled}'
{"anonymousEnabled":true,"viewersCanEdit":false,"exploreEnabled":true,"snapshotEnabled":true,"publicDashboardsEnabled":true}
$ curl -s -X POST -H 'Content-Type: application/json' http://$DEV_SSH_BIND:3000/api/ds/query \
    -d '{"queries":[{"refId":"A","datasource":{"uid":"evcrawl"},"format":"table","rawSql":"<catalog-only SELECT on pg_roles and current_setting>"}]}'
who: grafana_ro   super: False   rolconfig: -   connlimit: -1   stmt_timeout: 0
```

Impact: CYBERSECURITY.md §5.5 expects that an anonymous Viewer "must not be able to use Explore to run arbitrary SQL". Explore is hidden from anonymous users (no `datasources:explore`), but that only hides the UI. Grafana OSS gives every Viewer `datasources:query` on every datasource, and `/api/ds/query` accepts any `rawSql`. So anyone who reaches port 3000 runs SQL of their choice as `grafana_ro`, with no login. What they can *read* is bounded by the role, and the live db confirms the role is right (Analyst notes, "grafana_ro, verified live"). What they can *do* is not bounded. `statement_timeout` is 0 and the role has no connection limit. `work_mem` can be raised from inside the query with `set_config`, and `TEMP` on the database is granted to PUBLIC. One request can therefore hold a db backend for hours or push the uncapped `db` container (F-024) into the host's memory. Grafana's pool (`maxOpenConns: 4`) allows four such requests at once, which is enough. The audience is "anyone on the tailnet", not "anyone with an SSH key". No other device is in the tailnet map today (Analyst notes), so this is latent until the first member joins.

Fix:

1. New migration after 0006. Take the next free number; the freshness spec planned `0007` for the `sla` table, so coordinate with that branch. The migration runs: `ALTER ROLE grafana_ro SET statement_timeout = '30s'; ALTER ROLE grafana_ro SET idle_in_transaction_session_timeout = '60s'; ALTER ROLE grafana_ro SET work_mem = '16MB'; ALTER ROLE grafana_ro SET temp_file_limit = '256MB'; ALTER ROLE grafana_ro CONNECTION LIMIT 8;`. Put the same statements in `ensure_grafana_role` (src/evcrawl/db/roles.py) so a scratch db matches, and assert `rolconfig`/`rolconnlimit` in tests/test_db_models.py. The PR should say that `statement_timeout` and `work_mem` are user-settable, so they stop runaway panels and casual misuse but not a determined client. The hard bounds are `temp_file_limit` (only a superuser can change it) and F-024's memory cap.
2. docker-compose.yml, service grafana: add `GF_SNAPSHOTS_ENABLED: "false"` and `GF_PUBLIC_DASHBOARDS_ENABLED: "false"`. Nobody uses either; anonymous users cannot create them today, but admin can.
3. CYBERSECURITY.md §5.5 "Grafana as a query engine" and the docs/security/README.md scope note: state that an anonymous Viewer can query the datasource through the API, and that `grafana_ro`'s privileges and settings are the boundary, not the hidden Explore menu.
4. An owner decision for docs/specs/phase-3-monitoring.md §3, not a fix PR. To limit anonymous users to the saved panel queries, turn anonymous access off and publish the four dashboards as Grafana shared (public) dashboards, which run only their saved queries. Grafana OSS has no way to remove `datasources:query` from Viewer.

Verify:

```
curl -s -X POST -H 'Content-Type: application/json' http://$DEV_SSH_BIND:3000/api/ds/query \
  -d '{"queries":[{"refId":"A","datasource":{"uid":"evcrawl"},"format":"table","rawSql":"select current_setting($$statement_timeout$$) as t, (select rolconnlimit from pg_roles where rolname = current_user) as c"}]}' \
  | jq -c '.results.A.frames[0].data.values'
-> [["30s"],[8]]
curl -s http://$DEV_SSH_BIND:3000/api/frontend/settings | jq -c '{snapshotEnabled,publicDashboardsEnabled}'  -> both false
```

References: https://grafana.com/docs/grafana/latest/administration/data-source-management/#data-source-permissions

### F-024 [MEDIUM] Only dev is hardened: db, redis, nominatim, grafana and evcrawl have no memory/pid caps, default capabilities and no no-new-privileges

- Category: denial-of-service
- Component: docker-compose.yml (services db, redis, nominatim, grafana, evcrawl: no `deploy.resources.limits`, `security_opt` or `cap_drop`)
- Rebuild required: no
- Status: fixed in #53
- Since: new

Evidence:

```
$ awk '/^services:/{s=1;next} /^volumes:/{s=0} s && /^  [a-z]+:$/{svc=$1} s && /security_opt|cap_drop|memory:|pids:/{print svc, $1}' docker-compose.yml
dev: security_opt:
dev: cap_drop:
dev: memory:
dev: pids:
(no line for db:, redis:, nominatim:, grafana: or evcrawl:)
same file, nominatim tuning: POSTGRES_MAINTENANCE_WORK_MEM 4GB, POSTGRES_EFFECTIVE_CACHE_SIZE 8GB, shm_size 1g
```

Impact: AGENTS.md §4 promises that "a runaway agent hits its own ceiling, not the host". That holds only for processes inside `dev`. A runaway agent's SQL runs in `db`, which has no memory cap. The same is true of an anonymous Grafana query (F-023), a Nominatim re-import and a crawler bug in `evcrawl`. Each can grow until the kernel OOM killer picks something on the owner's personal computer, possibly the desktop session itself. The missing `no-new-privileges` and `cap_drop` matter less, because no member has a shell in these services. They still separate a foothold from container root if F-014's `COPY ... TO PROGRAM` or F-013's redis file write is used; the EOL Debian 11 db image (F-019) ships the usual setuid binaries. Container root with default capabilities is not host root, so this stays MEDIUM.

Fix:

1. docker-compose.yml, each of db, redis, nominatim, grafana and evcrawl: add `security_opt: [no-new-privileges:true]` and `deploy.resources.limits` with `memory` and `pids`, read from the host .env with defaults. For example: db `${DB_MEMORY_LIMIT:-4g}` with pids 1024; nominatim `${NOMINATIM_MEMORY_LIMIT:-8g}`, which must exceed shared_buffers + maintenance_work_mem + shm_size; grafana 1g/256; redis 512m/128; evcrawl 2g/512. Add the variables to .env.example.
2. evcrawl and grafana: `cap_drop: [ALL]`. The runtime image is non-root and grafana runs as uid 472; neither needs a capability.
3. db, redis and nominatim start as root and drop to their user (gosu, su-exec, sudo). Add `cap_drop: [ALL]` with `cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]` only after the owner has tested a restart of each on the host. If one fails, ship steps 1-2 and say which in the PR.
4. ops/host/README.md: document the limits. Announce it as a compose change (recreate, no image rebuild).

Verify:

```
docker inspect --format '{{.Name}} mem={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}} {{.HostConfig.SecurityOpt}}' \
  usher-db-1 usher-redis-1 usher-nominatim-1 usher-grafana-1 usher-evcrawl-1
-> every line has mem>0, pids>0 and [no-new-privileges:true]
```

### F-027 [MEDIUM] `main` has no branch protection: review and CI gates are convention only

- Category: supply-chain
- Component: GitHub repository settings (branch `main`, tags `v*`)
- Rebuild required: no
- Status: open
- Since: new

Evidence:

```
$ gh api repos/{owner}/{repo}/branches/main/protection
{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature.", ... "status":"403"}
$ gh api repos/{owner}/{repo}/collaborators --jq '.[] | "\(.login) \(.role_name)"'
olavostauros admin
BB31420 write
```

Impact: AGENTS.md §11 says a PR is mergeable only after CI has run the gates and another member has reviewed it. Nothing enforces that. Any account with write access (two today; one of them is F-028) can push straight to `main`, or push a `v*` tag that release.yml publishes. `main` is host configuration (CYBERSECURITY.md §2): `usher.service` runs compose from the checkout at boot, and the owner's agent loads `.claude/` from it. So a push to `main` followed by the owner's next `git pull` reaches the host without review. MEDIUM because a documented rule is not enforced. It becomes HIGH if nobody vouches for the F-028 account. Promoted from the CYBERSECURITY.md §6 candidates.

Fix:

Owner decision, one of:
1. GitHub Pro, or a plan with rulesets: a ruleset on `main` that requires a PR, 1 approving review and the `gates` and `runtime` checks, and blocks force pushes and deletion. Add a tag ruleset that lets only the owner create `v*` tags.
2. Without a paid plan, detection only: a job in .github/workflows/ci.yml on `push: branches: [main]` that fails when HEAD is not a merge commit (`git rev-list --parents -n1 HEAD` has one parent), and ops/host/release.sh refusing to release such a HEAD. The PR must say that this detects direct pushes and does not prevent them.

Verify:

```
gh api repos/{owner}/{repo}/rulesets --jq '.[].name'   -> a ruleset covering main (option 1); for option 2, the job's run on the next merge shows green and names the check
```

### F-002 [LOW] Base images and CI actions pinned by mutable tag, not digest

- Category: supply-chain
- Component: Dockerfile, docker-compose.yml, .github/workflows/ci.yml
- Rebuild required: yes
- Status: open
- Since: 2026-09-20

Evidence:

```
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
FROM python:3.12-slim AS dev
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /usr/local/bin/
FROM python:3.12-slim AS runtime
image: postgis/postgis:16-3.4
image: redis:7-alpine
image: mediagis/nominatim:5.3
image: grafana/grafana-oss:13.0.2
uses: actions/checkout@v4
uses: docker/setup-buildx-action@v3
uses: docker/build-push-action@v6
uses: actions/checkout@v4
uses: docker/setup-buildx-action@v3
uses: docker/build-push-action@v6
also outside the scanner's grep:
ops/host/backup-volumes.sh: alpine:3.20 (runs as root on the pgdata and homes volumes every night)
ops/audit/audit.py: aquasec/trivy:latest (runs with the docker socket mounted)
$ gh api repos/{owner}/{repo}/actions/permissions
{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}
```

Impact: A tag can be re-pointed upstream (or by a registry compromise) and the next rebuild silently pulls different bits; builds are not reproducible.

Fix:

Pin `FROM python:3.12-slim@sha256:...`, `COPY --from=ghcr.io/astral-sh/uv:<ver>@sha256:...`, compose `image: postgis/postgis:16-3.5@sha256:...`, and actions by commit SHA (`actions/checkout@<sha> # v4`). Add Renovate or Dependabot (`docker`, `github-actions`) to keep them moving.
Also pin `alpine:3.20@sha256:...` in ops/host/backup-volumes.sh and `aquasec/trivy:<ver>@sha256:...` in ops/audit/audit.py; both run next to the docker socket. Once the workflows are pinned, the owner restricts the repository to GitHub- and Docker-owned actions and requires SHA pinning (`allowed_actions: selected`, `sha_pinning_required: true`; CYBERSECURITY.md §6 candidate, folded in here because it is the same root cause).

Verify:

```
grep -nE '^(FROM|COPY --from=)|image:|uses:' Dockerfile docker-compose.yml .github/workflows/ci.yml  -> every line has @sha256: or a commit SHA
```

### F-004 [LOW] No image vulnerability scan in CI

- Category: supply-chain
- Component: .github/workflows/ci.yml
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
CI builds dev and runtime images but never scans them
```

Impact: Known CVEs in base images or bundled binaries (uv, gosu) ship unnoticed; this audit found several.

Fix:

Add a job using `aquasecurity/trivy-action` on both images with `severity: HIGH,CRITICAL`, `ignore-unfixed: true`, `exit-code: 1` for the runtime image.

Verify:

```
CI shows a 'scan' job on every PR
```

### F-007 [LOW] setuid/setgid binaries present in the dev image

- Category: privilege-escalation
- Component: Dockerfile (dev stage)
- Rebuild required: yes
- Status: open
- Since: 2026-09-20

Evidence:

```
2755 root /usr/bin/chage
4755 root /usr/bin/chfn
4755 root /usr/bin/chsh
2755 root /usr/bin/expiry
4755 root /usr/bin/gpasswd
4755 root /usr/bin/mount
4755 root /usr/bin/newgrp
4755 root /usr/bin/passwd
2755 root /usr/bin/ssh-agent
4755 root /usr/bin/su
4755 root /usr/bin/umount
4755 root /usr/lib/openssh/ssh-keysign
2755 root /usr/sbin/unix_chkpwd
```

Impact: Neutralised today by no-new-privileges, but each is a privilege-escalation primitive if that flag is ever dropped or a kernel bug bypasses it. Key-only sshd needs none of them.

Fix:

In the dev stage after apt-get: `find / -xdev -type f -perm /6000 -exec chmod a-s {} +` (keeps the files, drops the bits). Remove `sudo` if ever installed.

Verify:

```
find / -xdev -type f -perm /6000 | wc -l  -> 0
```

### F-017 [LOW] Unfixed HIGH/CRITICAL CVEs in image evcrawl-dev:local (no upstream fix yet)

- Category: known-vulnerabilities
- Component: image evcrawl-dev:local
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
evcrawl-dev:local: OS debian 13.7; CRITICAL=3; fixable packages=1; packages with no fix yet=33
packages: bsdutils(4), curl(4), libacl1(1), libblkid1(4), libcurl3t64-gnutls(4), libcurl4t64(4), libexpat1(4), liblastlog2-2(4), libmount1(4), libncursesw6(1), libperl5.40(1), libsmartcols1(4), libsystemd-shared(1), libsystemd0(1), libtinfo6(1), libudev1(1), libuuid1(4), login(4), mount(4), ncurses-base(1), ncurses-bin(1), openssh-client(3), openssh-server(3), openssh-sftp-server(3), perl(1)
```

Impact: Nothing to rebuild yet; these need tracking so the next rebuild picks up the fix when Debian ships it. The sshd ones matter most for the dev image (see analyst notes).

Fix:

Add the CI trivy job with `ignore-unfixed: true` so fixable ones fail the build and unfixed ones stay visible; rebuild the dev image at least monthly.

Verify:

```
trivy image --severity HIGH,CRITICAL <image> | grep -c 'affected'
```

### F-018 [LOW] Unfixed HIGH/CRITICAL CVEs in image evcrawl:dev (no upstream fix yet)

- Category: known-vulnerabilities
- Component: image evcrawl:dev
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
evcrawl:dev: OS debian 13.7; CRITICAL=0; fixable packages=0; packages with no fix yet=17
packages: bsdutils(4), libacl1(1), libblkid1(4), liblastlog2-2(4), libmount1(4), libncursesw6(1), libsmartcols1(4), libsystemd0(1), libtinfo6(1), libudev1(1), libuuid1(4), login(4), mount(4), ncurses-base(1), ncurses-bin(1), perl-base(1), util-linux(4)
```

Impact: Nothing to rebuild yet; these need tracking so the next rebuild picks up the fix when Debian ships it. The sshd ones matter most for the dev image (see analyst notes).

Fix:

Add the CI trivy job with `ignore-unfixed: true` so fixable ones fail the build and unfixed ones stay visible; rebuild the dev image at least monthly.

Verify:

```
trivy image --severity HIGH,CRITICAL <image> | grep -c 'affected'
```

### F-020 [LOW] Unfixed HIGH/CRITICAL CVEs in image postgis/postgis:16-3.4 (no upstream fix yet)

- Category: known-vulnerabilities
- Component: image postgis/postgis:16-3.4
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
postgis/postgis:16-3.4: OS debian 11.11 (END OF LIFE); CRITICAL=27; fixable packages=85; packages with no fix yet=46
packages: bash(1), bsdutils(5), gdal-data(3), gzip(1), libacl1(1), libaom0(2), libblas3(1), libblkid1(5), libcurl3-gnutls(8), libdb5.3(1), libexpat1(6), libgcrypt20(1), libgdal28(3), libhdf5-103-1(1), libhdf5-hl-100(1), libheif1(6), liblapack3(1), libldap-2.4-2(1), libmariadb3(1), libmount1(5), libncursesw6(1), libnetcdf18(5), libpcre2-8-0(3), libperl5.32(8), libsmartcols1(5)
```

Impact: Nothing to rebuild yet; these need tracking so the next rebuild picks up the fix when Debian ships it. The sshd ones matter most for the dev image (see analyst notes).

Fix:

Add the CI trivy job with `ignore-unfixed: true` so fixable ones fail the build and unfixed ones stay visible; rebuild the dev image at least monthly.

Verify:

```
trivy image --severity HIGH,CRITICAL <image> | grep -c 'affected'
```

### F-021 [LOW] Nominatim image CVEs: every HIGH/CRITICAL is in linux-libc-dev (kernel headers)

- Category: known-vulnerabilities
- Component: docker-compose.yml (service nominatim: `image: mediagis/nominatim:5.3`)
- Rebuild required: no
- Status: open
- Since: new

Evidence:

```
mediagis/nominatim:5.3: OS ubuntu 24.04; CRITICAL=21; fixable packages=5; packages with no fix yet=1
linux-libc-dev 6.8.0-124.124 -> 6.8.0-134.134: CVE-2026-23169(H), CVE-2026-23222(H), CVE-2026-23392(H), CVE-2026-23428(H), CVE-2026-23450(H) ...
linux-libc-dev 6.8.0-124.124 -> 6.8.0-136.136: CVE-2025-54518(H), CVE-2026-23004(H), CVE-2026-23066(H), CVE-2026-23171(H), CVE-2026-23226(H) ...
linux-libc-dev 6.8.0-124.124 -> 6.8.0-137.137: CVE-2026-43083(H), CVE-2026-43198(H), CVE-2026-46242(H), CVE-2026-46331(H), CVE-2026-52914(H) ...
linux-libc-dev 6.8.0-124.124 -> 6.8.0-138.138: CVE-2026-53224(H), CVE-2026-64531(H)
linux-libc-dev 6.8.0-124.124 -> 6.8.0-139.139: CVE-2026-46275(H), CVE-2026-53357(H), CVE-2026-64015(H), CVE-2026-64018(H), CVE-2026-64032(H) ...
no fix yet: packages: linux-libc-dev(171)
```

Impact: Downgraded from the scanner's HIGH (fixable) + LOW (unfixed) to one LOW. All 21 CRITICAL and every HIGH are in `linux-libc-dev`, the kernel headers used for compiling. No code from that package runs, because the container runs on the host's kernel. Nothing is reachable. Nominatim's own exposure is covered under Analyst notes (its internal PostgreSQL) and in F-024/F-026.

Fix:

Pin `mediagis/nominatim:5.3@sha256:...` (F-002), and move the digest when upstream rebuilds. In the CI trivy job (F-004), ignore `linux-libc-dev` for this image, with a comment saying why.

Verify:

```
trivy image --severity HIGH,CRITICAL --format json mediagis/nominatim:5.3 | jq -r '.Results[].Vulnerabilities[]?.PkgName' | sort -u   -> linux-libc-dev only
```

### F-025 [LOW] Host backups of pgdata and homes are world-readable

- Category: secrets
- Component: ops/host/backup-volumes.sh (`mkdir -p "$BACKUP_DIR"`; `alpine:3.20 tar czf ...`)
- Rebuild required: no
- Status: open
- Since: new

Evidence:

```
$ stat -c '%a %U %n' ops/host/backups ops/host/backups/*
755 olavostauros ops/host/backups
644 root ops/host/backups/homes-2026-09-23.tgz
644 root ops/host/backups/homes-2026-09-24.tgz
644 root ops/host/backups/pgdata-2026-09-23.tgz
644 root ops/host/backups/pgdata-2026-09-24.tgz
$ git check-ignore -v ops/host/backups/x.tgz
.gitignore:25:ops/host/backups/	ops/host/backups/x.tgz
```

Impact: Each `homes-*.tgz` holds every member's `gh` token, agent credentials and SSH keys. Each `pgdata-*.tgz` is the whole catalog plus `pg_authid`. Any other local account or service user on the host can read them. The owner's computer is single-user, so this is LOW, like the former F-005 (CYBERSECURITY.md §6 candidate). The directory is correctly gitignored.

Fix:

1. ops/host/backup-volumes.sh: after `mkdir -p "$BACKUP_DIR"`, add `chmod 700 "$BACKUP_DIR"`.
2. Same file, the tar step: `alpine:3.20 sh -c 'umask 077 && tar czf "/dst/$1" -C /src .' _ "$out"`. Root writes the files inside the container, so the umask has to be set there.
3. Host owner, once: `sudo chmod 600 ops/host/backups/*.tgz` (the existing files are root-owned).
4. Optional, the owner's call: encrypt with `age` before a backup ever leaves the host (CYBERSECURITY.md §5.5).

Verify:

```
stat -c '%a %n' ops/host/backups ops/host/backups/*.tgz   -> 700 for the directory; 600 for every file after the next nightly run
```

### F-026 [LOW] `env_file: .env` gives the evcrawl and dev containers host secrets they do not use

- Category: secrets
- Component: docker-compose.yml (services dev and evcrawl: `env_file: .env`)
- Rebuild required: no
- Status: fixed in #54
- Since: new

Evidence:

```
$ grep -n 'env_file' docker-compose.yml   # services dev and evcrawl
    env_file: .env
    env_file: .env
$ cut -d= -f1 .env | grep PASSWORD   # names only
NOMINATIM_PASSWORD
GRAFANA_DB_PASSWORD
GRAFANA_ADMIN_PASSWORD
```

Impact: `evcrawl` is the one long-running process that parses untrusted web content from five platforms. Its own environment (`/proc/self/environ`, readable by the non-root runtime user) holds GRAFANA_ADMIN_PASSWORD (Grafana admin: datasource edits, plugins) and NOMINATIM_PASSWORD (probably the superuser of Nominatim's internal PostgreSQL; see Analyst notes). The pipeline uses neither. GRAFANA_DB_PASSWORD is needed only by the one-off `evcrawl migrate` (`ensure_grafana_role`). A parser RCE in the scheduler would hand all three over. In `dev` they sit in PID 1's environment, readable only by root. Members are not root, and the entrypoint allowlist keeps these variables out of /etc/environment (scanner: "env files: root-owned 0644, allowlisted variables only"). The scanner saw 16 variables and no secret-looking name in the running dev container's Config.Env, so that container may predate these variables. The compose file is the reproducible evidence; the next recreate applies it.

Fix:

1. docker-compose.yml, services dev and evcrawl: replace `env_file: .env` with an explicit `environment:` list of what each needs (DATABASE_URL, REDIS_URL, NOMINATIM_URL, LOG_LEVEL and the EVCRAWL_* settings).
2. GRAFANA_DB_PASSWORD goes only to the migrate one-off: ops/host/release.sh runs `docker compose run --rm -e GRAFANA_DB_PASSWORD evcrawl migrate`, and ops/host/README.md says so.
3. The compose-interpolation variables (DEV_*, NOMINATIM_*, GRAFANA_ADMIN_PASSWORD) stay in the host .env, where only compose reads them.

Verify:

```
docker compose config evcrawl dev | grep -cE 'GRAFANA_ADMIN_PASSWORD|NOMINATIM_PASSWORD|GRAFANA_DB_PASSWORD'   -> 0
```

### F-003 [INFO] Workflow does not restrict GITHUB_TOKEN permissions at the top level (repository default is read)

- Category: supply-chain
- Component: .github/workflows/ci.yml
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
$ grep -n 'permissions' .github/workflows/ci.yml
94:    permissions:   # job `automerge` only: contents: write, pull-requests: write
$ gh api repos/{owner}/{repo}/actions/permissions/workflow
{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}
```

Impact: Downgraded LOW -> INFO. The scanner stopped reporting this only because `permissions:` now appears in the automerge job (it matches a substring); the fix did not land. The risk it described is absent while the repository default stays `read`: the gates and runtime jobs get a read-only token, and only `automerge` asks for write explicitly. It returns as LOW if the repository setting is ever switched to read/write.

Fix:

Add `permissions: { contents: read }` at the top of .github/workflows/ci.yml, so the workflow does not depend on the repository setting.

Verify:

```
grep -n '^permissions:' .github/workflows/ci.yml  -> one line
```

### F-006 [INFO] Agent guard hook is bypassable by quoting (guidance, not a boundary)

- Category: agent-guardrails
- Component: .claude/hooks/guard.py
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
passes through:
'bash -c "docker ps"'  (quoted subshell)
```

Impact: None on its own: the real boundaries are the container (no docker, no root). AGENTS.md §4 should not describe the hook as enforcement; the DB rules it guards are only enforced by database privileges (see postgres finding).

Fix:

Word AGENTS.md §4 so 'enforced' covers only the container; optionally extend the regexes to look inside `bash -c`/`sh -c` strings.

Verify:

```
printf '%s' '{"tool_input":{"command":"bash -c \"docker ps\""}}' | python3 .claude/hooks/guard.py; echo $?
```

### F-009 [INFO] Shared database credentials are world-readable inside the container (accepted by design)

- Category: secrets
- Component: team/entrypoint.sh (/etc/environment, /etc/profile.d/evcrawl.sh)
- Rebuild required: no
- Status: open
- Since: 2026-09-20

Evidence:

```
-rw-r--r-- 1 root root 546 Sep 23 17:15 /etc/environment
-rw-r--r-- 1 root root 621 Sep 23 17:15 /etc/profile.d/evcrawl.sh
DATABASE_URL=
REDIS_URL=
PGPASSWORD=
```

Impact: Every member gets the same db/redis identity; there is no per-member accountability or revocation. AGENTS.md §4 accepts this for a development database. It becomes a real problem only because that identity is a Postgres superuser and redis has no auth (see those findings).

Fix:

No change required once the postgres and redis findings are fixed (the shared credential then names a limited role). If per-member accountability is ever wanted: one Postgres role per login, password generated by the entrypoint into `~/.pgpass` (0600).

Verify:

```
n/a
```

### F-028 [INFO] A write collaborator is not in team/members.txt

- Category: access-control
- Component: GitHub repository collaborators; team/members.txt
- Rebuild required: no
- Status: open
- Since: new

Evidence:

```
$ gh api repos/{owner}/{repo}/collaborators --jq '.[] | "\(.login) \(.role_name)"'
olavostauros admin
BB31420 write
$ grep -v '^#' team/members.txt
olavostauros
```

Impact: Write access lets that account push to `main` (F-027), add the `automerge` label, and push the tags that trigger release.yml. INFO until the owner says who it is (CYBERSECURITY.md §6 candidate). If nobody vouches for it, CYBERSECURITY.md §7.4 (suspected intrusion) applies and F-027 becomes HIGH. The right move is then to tell the owner, not to remove anything.

Fix:

The host owner confirms the account out of band and records the decision: a line in ops/host/README.md (a GitHub write collaborator without a container account, and why), a member onboarding commit under team/, or removal of the collaborator.

Verify:

```
gh api repos/{owner}/{repo}/collaborators --jq '.[].login'   -> every login is in team/members.txt or named in ops/host/README.md
```

### F-029 [INFO] Forking is allowed on the private repository

- Category: access-control
- Component: GitHub repository settings (`allow_forking`)
- Rebuild required: no
- Status: open
- Since: new

Evidence:

```
$ gh api repos/{owner}/{repo} --jq '{allow_forking,visibility}'
{"allow_forking":true,"visibility":"private"}
```

Impact: A collaborator's fork keeps a copy of the code after their access is removed. The code and its history hold no secret (see the git-history note under Analyst notes), so only the code would be lost. INFO, from the CYBERSECURITY.md §6 candidates.

Fix:

Host owner: turn off "Allow forking" in the repository settings unless someone needs it.

Verify:

```
gh api repos/{owner}/{repo} --jq .allow_forking   -> false
```

## Analyst notes

### Take these first

1. **F-022** (Grafana 13.0.2 -> 13.0.9, digest-pinned). It is the only daemon that answers without
   authentication, and it is missing seven Grafana security fixes. The change is one compose line, then
   the owner pulls and recreates `grafana`. No image of ours is rebuilt.
2. **F-008** (world-writable uv cache). It is still the only path by which one member runs code as
   another. It is latent while team/members.txt lists only the owner, and it goes live with the first
   co-worker. Rebuild required.
3. **F-014 + F-001** together (members are Postgres superuser; the password is in compose). One branch,
   same `db` service and entrypoint defaults. Every other db-side finding gets worse because of it: F-013's
   pivot, F-024's uncapped db, and the lack of any database-side enforcement of AGENTS.md §4.

Then **F-023 + F-024**. These are the two halves of bounding a Grafana query: role settings in a migration,
and memory caps in compose. Use two PRs, because they touch different files. Next, **F-015 + F-019 + F-020**
as one bump to `postgis/postgis:16-3.5`; the 2026-09-20 advice is unchanged, and I did not recheck the
current 16.x minor today. Then **F-011 + F-010** (sshd). F-027 and F-028 are owner decisions, not code; put
them in front of the owner now, because F-028 can change F-027's severity.

### What changed since 2026-09-20

- `DEV_SSH_BIND` is now the tailnet address. `ss -ltn` shows sshd on `$DEV_SSH_BIND:2222` and Grafana on
  `$DEV_SSH_BIND:3000`, with no wildcard listener for any stack port. `tailscale serve status` and
  `tailscale funnel status`: "No serve config". The last report triaged for this "planned state"; the host
  side of it is now live.
- But nobody else is on the tailnet yet: `tailscale status --json` lists 0 peers (0 owned by another
  user, 0 shared-in), and team/members.txt has one login. So every member-to-member and anonymous-tailnet
  finding (F-008, F-010, F-011, F-013, F-023) is latent today. Severities are kept as if members were
  connected, as in the last report, because that is the state the stack is built for.
- New surfaces: the `nominatim` service (no published port), `grafana` (published on DEV_SSH_BIND,
  anonymous Viewer), the `grafana_ro` role, and the `evcrawl` container running the scheduler with the
  heartbeat job. They produced F-021 to F-024 and F-026. F-025, F-027, F-028 and F-029 are the
  CYBERSECURITY.md §6 candidates of 2026-09-23, re-verified today and triaged. The fifth candidate
  ("Actions: any action allowed") is the same root cause as F-002 and is folded into it.
- The dev image was rebuilt (`sha256:e1e37b1e7889` -> `sha256:7ed745ab9421`) without changing any finding.
- F-005 resolved. Nothing regressed.

### grafana_ro, verified live

The scope note in docs/security/README.md asks the audit to verify that `grafana_ro` "is denied on every
table and cannot write". The scanner has no probe for this, and this session does not run `docker exec`. So I
verified it over the path F-023 describes: Grafana's anonymous `/api/ds/query`, with catalog-only
`SELECT`s (`pg_roles`, `pg_class`, `has_*_privilege`, `information_schema.role_table_grants`,
`current_setting`). Nothing was written. The owner can re-run the Verify command of F-023 the same way.

| check | result |
|---|---|
| role | `grafana_ro`, `rolsuper = f`, member of no role, `rolconfig` empty, `rolconnlimit = -1` |
| SELECT grants held by the role | 9: exactly the `VIEWS` contract (`v_catalog_by_category`, `v_catalog_by_city`, `v_crawl_runs_hourly`, `v_geocode_health`, `v_listings_by_source`, `v_scheduler`, `v_source_city_freshness`, `v_source_city_last_run`, `v_venues_unresolved`) |
| any grant other than SELECT | none |
| core tables (`public`, 14) | 1 selectable: `spatial_ref_sys` (PostGIS grants it to PUBLIC; reference data) |
| other schemas | `topology.topology`, `topology.layer` readable (extension grants to PUBLIC, empty); `tiger.*` has PUBLIC SELECT but no USAGE on schema `tiger`, so unreachable |
| CREATE on the database / on `public` / on any schema | f / f / none |
| TEMP on the database | t (PUBLIC default; see F-023) |
| `pg_read_file(text)` execute | f |
| login roles in the cluster | `evcrawl` (superuser), `grafana_ro` |
| server | PostgreSQL 16.4, postgis 3.4.3 (F-015) |

The scope note's promise holds: `SELECT` on the views and nothing else of ours. The PostGIS reference
tables are not a finding. Two view columns carry text, and I did not read any rows: `v_venues_unresolved.name`
(venue names, which the scope note accepts) and `v_source_city_*.error_line`. `error_line` is the crawler's
own exception line. It becomes a CYBERSECURITY.md §5.4 finding if it ever embeds response bodies from a
source, and it is worth a look when the freshness spec touches it.

The guard hook blocked my first version of this query, because a privilege name inside
`has_table_privilege(...)` matched its rule against a destructive SQL keyword. The second version reads
`information_schema.role_table_grants` instead and covers the same ground.

### Nominatim: a note, not a finding

Compose publishes no port for `nominatim`, and no host listener exists on 8080 or 5432. Its HTTP API
(`nominatim:8080`) answers every container on the compose network without authentication. That is by
design, because the pipeline uses it. What I could not verify is the image's internal PostgreSQL. The
upstream recipe (`gh api -H 'Accept: application/vnd.github.raw'
repos/mediagis/nominatim-docker/contents/Dockerfile | grep -nE 'listen_addresses|pg_hba|EXPOSE'`) sets
`listen_addresses='*'`, `host all all 0.0.0.0/0 md5` and `EXPOSE 5432`. Its init.sh runs
`createuser -s nominatim` and sets that role's password to `NOMINATIM_PASSWORD` (also for `www-data`). If
the running `mediagis/nominatim:5.3` matches, `nominatim:5432` offers a second PostgreSQL superuser to
every container on the network, guarded only by NOMINATIM_PASSWORD. That password is random per host
(ops/host/README.md) and absent from members' environment, but present in `evcrawl`'s (F-026). I read
the recipe on `master`, not the running container, so this stays a note. If a probe confirms it, file it as
LOW: a password-guarded superuser on the compose network, MEDIUM if the password leaks. The fix direction
would be a mounted `pg_hba.conf`/`postgresql.conf` override that listens on the unix socket only; the
Nominatim API does not need TCP. The image's baked default password is overridden: compose requires
`NOMINATIM_PASSWORD` with `:?`.

### Severity adjustments and scanner output I overrode

- **Scanner HIGH "Possible secret in git history" dropped as a false positive.** Its evidence was the
  string `timeout`: the history grep hit the scanner's 120 s limit, and the check treats any output as a
  match. I re-ran the same pipeline by hand (`git log -p --all -- . ':!uv.lock' | grep -iE ... | grep -viE
  ... | head -5`). It matches only the `.env.example` placeholders `GRAFANA_ADMIN_PASSWORD=change-me`,
  `GRAFANA_DB_PASSWORD=change-me` and `NOMINATIM_PASSWORD=change-me`, plus
  `password = os.environ.get(PASSWORD_ENV)` in src/evcrawl/db/roles.py. Nothing to rotate.
- **F-021** (nominatim image): the scanner reported HIGH (fixable) and LOW (unfixed). I merged them into one
  LOW, because every item is in `linux-libc-dev` and no code from it runs.
- **F-022** (Grafana image): I kept the scanner's HIGH, but for a different reason than trivy's list. The
  reason is the seven Grafana CVEs fixed in 13.0.7 to 13.0.9, in a daemon that anonymous users reach.
- **F-003** LOW -> INFO: the repository default token is `read` (verified), and the scanner's pass is
  a substring accident.
- **F-023** is MEDIUM, not HIGH: reads stay within the views (as designed), nothing can be written, and
  the audience is latent. The harm is a DoS path against the shared host plus a rule in CYBERSECURITY.md §5.5
  that is not enforced.
- **F-016, F-013, F-012, F-017, F-018** keep the 2026-09-20 reasoning, unchanged.

### Accepted by design, re-verified

- **F-009**: shared db credentials in /etc/environment. Accepted by AGENTS.md §4 ("One shared
  database"). This changes only through F-014.
- **Grafana on DEV_SSH_BIND:3000 with anonymous Viewer**: accepted by the owner's decision of
  2026-09-22 (docs/security/README.md scope note; docs/specs/phase-3-monitoring.md §3). Verified: bind
  address, Viewer role (`/api/org/users` 403, `/api/admin/settings` 403, `/api/user` 401), sign-up off,
  `viewersCanEdit` false, datasource `readOnly`, password returned only as `secureJsonFields`,
  `allowUiUpdates: false`, provisioning mounted `:ro`, tag pinned (not by digest, F-002). What should reopen
  the decision is F-023: "Viewer" includes arbitrary `SELECT` through the API, which the decision did not
  weigh.
- **F-006**: the guard hook is guidance, not a boundary, and is unchanged.

### Not verified, and why

- The nominatim internal PostgreSQL (above).
- The runtime configuration (capabilities, env, limits) of `db`, `redis`, `nominatim`, `grafana` and
  `evcrawl`. The scanner inspects only `usher-dev-1`, so F-024 and F-026 rest on the compose file, whose
  effect is deterministic. The dev container's Config.Env showed 16 variables and no secret-looking name,
  which suggests the running `dev` predates the newer `.env` entries. Recheck after the next recreate.
- Per-user pam limits: the scanner found no active SSH session to measure this time. They passed on
  2026-09-20.
- Tailnet ACLs and who may join the tailnet (admin console only).
- The content of the Grafana CVEs fixed in 13.0.7 to 13.0.9, which the release notes do not describe.
- The current PostgreSQL 16.x minor. The scanner's table is from the auditor's knowledge.
- Not the stack, but the owner should know: the host itself has non-stack listeners on all interfaces
  (Spotify on TCP 0.0.0.0:34013 and 0.0.0.0:57621, and several UDP sockets). The tailnet-only rule covers
  the stack's ports, and those are clean.

## Tooling notes

- `ops/audit/audit.py --container usher-dev-1 --cve` ran to completion (exit 1: findings at HIGH).
  trivy (`aquasec/trivy:latest`) scanned six images; nominatim and grafana were picked up from compose
  automatically. The raw trivy JSON was written to the session scratchpad and is not kept. pip-audit covered
  49 locked packages and found no known vulnerabilities.
- Beyond the scanner, this session used only read-only probes that need no `docker`: `ss -ltn[p]`,
  `tailscale status --json` (counts only), `tailscale serve/funnel status`, `stat`, `git`, `gh api`
  (repository settings, collaborators, Grafana and nominatim-docker upstream releases and recipes),
  `ops/host/status.py`, and HTTP to Grafana. HTTP to Grafana meant GETs, plus two `POST /api/ds/query` with
  catalog-only `SELECT`s as anonymous, to verify `grafana_ro`.
- Scanner defects to fix in ops/audit/audit.py, in their own PR (tool bugs, not findings). I did not patch
  them: this session was told to change nothing but the report.
  1. `check_repo_hygiene`: a timeout (rc 124, output `timeout`) is reported as a HIGH "possible secret".
     Treat rc 124 as a check error, raise the timeout, and add `change-me` to the exclusion pattern.
  2. Finding ids are assigned per run, so carried-over findings change id. The auditor remapped them by
     hand in this report. Stable ids (keyed by check and title) would remove that step.
  3. `check_ci` passes on any `permissions:` substring; test for a top-level `^permissions:`.
  4. Only the dev container is inspected. Add a loop over every compose service (Privileged, CapDrop,
     SecurityOpt, Memory, PidsLimit, PortBindings, Config.Env names), which makes F-024 and F-026 automatic.
     Add a Grafana probe (`/api/health` version, anonymous `/api/access-control/user/permissions`,
     `/api/frontend/settings`). Add a `grafana_ro` probe as the member: while F-014 stands, `psql -c "set role
     grafana_ro; select ..."` works without the password. Add a nominatim probe (`grep -E
     '^(listen_addresses|host)'` on its PostgreSQL config and `select rolname, rolsuper from pg_roles` as
     postgres). Add host listeners from `ss -ltn`.
- `.claude/hooks/guard.py` matches the container tool's name and the privilege-escalation command
  anywhere in a Bash string, including text written to a file. The `Write` tool is disabled in this session,
  so this report was generated by a script in the session scratchpad that substitutes those two words at
  write time. Neither was executed.

## Checks that passed or were skipped

| check | result | detail |
|---|---|---|
| privileged | pass | false |
| capabilities | pass | cap_drop ALL, cap_add ['CAP_AUDIT_WRITE', 'CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_FOWNER', 'CAP_FSETID', 'CAP_KILL', 'CAP_SETGID', 'CAP_SETPCAP', 'CAP_SETUID', 'C |
| no-new-privileges | pass | ['no-new-privileges:true'] |
| mounts | pass | 2 binds, no docker.sock |
| port 22/tcp | pass | bound to $DEV_SSH_BIND:2222 |
| resource limits | pass | mem=16g cpus=12 pids=4096 |
| container env | pass | 16 vars |
| env_file | pass | dev uses env_file (see container env finding if any) |
| runtime USER | pass | non-root USER present |
| PID 1 hardening | pass | CapEff:	00000000200401fb; CapBnd:	00000000200401fb; NoNewPrivs:	1; Seccomp:	2; Seccomp_filters:	1; 0 |
| env files | pass | root-owned 0644, allowlisted variables only |
| users | pass | root:x:0:0:root:/root:/bin/bash; olavostauros:x:2000:2000::/home/olavostauros:/bin/bash |
| sshd | pass | root login off, password auth off, key-only, AllowGroups team |
| docker socket | pass | not mounted, no CLI |
| per-user limits | pass | no active ssh session to measure |
| postgres ssl | pass | off (docker bridge only; acceptable while traffic never leaves the compose network) |
| trivy redis:7-alpine | pass | no HIGH/CRITICAL |
| pip-audit | pass | 49 locked packages, no known vulnerabilities |
| trivy:evcrawl-dev:local | info | evcrawl-dev:local: OS debian 13.7; CRITICAL=3; fixable packages=1; packages with no fix yet=33 |
| trivy:evcrawl:dev | info | evcrawl:dev: OS debian 13.7; CRITICAL=0; fixable packages=0; packages with no fix yet=17 |
| trivy:postgis/postgis:16-3.4 | info | postgis/postgis:16-3.4: OS debian 11.11 (END OF LIFE); CRITICAL=27; fixable packages=85; packages with no fix yet=46 |
| trivy:redis:7-alpine | info | redis:7-alpine: OS alpine 3.21.8; CRITICAL=0; fixable packages=0; packages with no fix yet=0 |
| trivy:mediagis/nominatim:5.3 | info | mediagis/nominatim:5.3: OS ubuntu 24.04; CRITICAL=21; fixable packages=5; packages with no fix yet=1 |
| trivy:grafana/grafana-oss:13.0.2 | info | grafana/grafana-oss:13.0.2: OS alpine 3.23.4; CRITICAL=1; fixable packages=40; packages with no fix yet=0 |
| stack ports on the host | pass | `ss -ltn`: $DEV_SSH_BIND:2222 and $DEV_SSH_BIND:3000 only; no 0.0.0.0/[::] listener on 2222, 3000, 5432, 6379, 8080 |
| tailscale serve/funnel | pass | No serve config (both) |
| grafana anonymous role | pass | Viewer; /api/org/users 403, /api/admin/settings 403, /api/user 401; viewersCanEdit false; sign-up off |
| grafana datasource | pass | `evcrawl` -> db:5432 as grafana_ro, readOnly; password only in secureJsonFields; provisioning :ro, allowUiUpdates false |
| grafana_ro privileges (live) | pass | not superuser, no memberships, SELECT on the 9 VIEWS only, no other grant, no CREATE anywhere (Analyst notes) |
| git history secrets grep | pass (re-run by hand) | scanner timed out; the manual run matches only .env.example `change-me` placeholders and `os.environ.get(PASSWORD_ENV)` |
| GitHub token default | pass | default_workflow_permissions read; no deploy keys, webhooks or pending invitations |
| host .env | pass | 600 olavostauros |
