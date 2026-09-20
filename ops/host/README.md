# Running the workspace host

The `dev` container from `docker-compose.yml` is the team's shared workspace
(AGENTS.md §4). This file is for whoever runs the host machine. Today that is
the owner's own computer; in Phase 1 it moves to the VPS.

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

# 3. Your own key (once)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -C usher
cp ~/.ssh/id_ed25519.pub team/keys/<your-login>.pub   # <your-login> is in team/members.txt

# 4. Build and start
docker compose build dev
docker compose up -d db redis dev
docker compose run --rm evcrawl migrate

# 5. Start on boot (user services keep running after logout)
loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp ops/host/usher.service ops/host/usher-backup.service ops/host/usher-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usher.service usher-backup.timer
```

Check: `ss -ltn | grep 2222` must show only the Tailscale IP (or 127.0.0.1),
never `0.0.0.0` or `*`.

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

## Backups and restore

`usher-backup.timer` runs `backup-volumes.sh` nightly at 03:30 and keeps 14
days under `ops/host/backups/` (gitignored). Restore a volume:

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

- `docker compose down -v` deletes the database **and every member's home**.
  Announce it, take a backup, wait for acknowledgements.
- Never change `DEV_SSH_BIND` to `0.0.0.0`. If Tailscale is down, the port is
  simply unreachable; that is the intended failure mode.
- Members have no host account and no sudo inside the container. Anything they
  need goes into the image via PR.
