# usher-prod VM setup (docs/DEPLOY.md step 6)

Owner only, by hand: this installs packages, joins the tailnet and places
secrets. Nothing here starts the stack; steps 7-9 do. Run the commands in
order. "host" is your computer, in your checkout of this repository; "VM"
is a shell on `usher-prod`, where the checkout is always `~/usher` (the
script's `CHECKOUT`, the unit's `WorkingDirectory`), never `~/Work`.

## 1. Copy the script to the VM and open a shell

```bash
# host
gcloud compute scp ops/gcp/vm-setup.sh usher-prod:~ \
  --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
gcloud compute ssh usher-prod --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
```

## 2. Packages, then the tailnet

If the tailnet still lists an offline `usher-prod` from a deleted VM, remove
that node in the Tailscale admin console first; otherwise the new VM joins
as `usher-prod-1` and the §4 `DEV_SSH_BIND` and the docs stop matching.

```bash
# VM
bash vm-setup.sh packages
exit            # log out so the docker group applies; ssh back in (step 1, second line)
sudo tailscale up --hostname=usher-prod   # opens a login URL: approve it in your browser
sudo tailscale set --accept-dns=false     # keep Google's resolver: MagicDNS cannot resolve the registry here
tailscale ip -4                           # the VM's tailnet address
getent hosts registry-1.docker.io         # prints addresses; empty means DNS is still Tailscale's
```

Without the `--accept-dns=false` line, Tailscale takes over `/etc/resolv.conf`
and every image pull fails with `lookup registry-1.docker.io ... server
misbehaving` (met 2026-10-07 at step 7). Nothing on the VM needs MagicDNS.

## 3. Deploy key and checkout

```bash
# VM
bash vm-setup.sh key          # prints an ssh-ed25519 public key
```

```bash
# host: a key of a deleted VM must go first (its private half is gone)
gh repo deploy-key list --repo olavostauros/usher
gh repo deploy-key delete <id> --repo olavostauros/usher        # only if one is listed
# paste the new key into a file and add it READ-ONLY (no --allow-write)
gh repo deploy-key add /path/to/usher_deploy.pub --repo olavostauros/usher --title usher-prod
```

```bash
# VM
bash vm-setup.sh clone        # ~/usher at main, prints the commit
```

## 4. Secrets

`.env` and the tunnel credentials go host → VM directly; they never pass
through an agent, a commit or a chat.

```bash
# host
gcloud compute scp .env usher-prod:~/usher/.env \
  --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
gcloud compute scp ops/host/cloudflared/credentials.json \
  usher-prod:~/usher/ops/host/cloudflared/credentials.json \
  --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
```

```bash
# VM
chmod 600 ~/usher/ops/host/cloudflared/credentials.json
bash ~/usher/ops/gcp/vm-setup.sh env       # DEV_SSH_BIND, CLOUDFLARED_USER, TEAM_ROLE_LOGIN=0, §4 limits
bash ~/usher/ops/gcp/vm-setup.sh service   # usher.service installed, not enabled
```

The copied `.env` carries the host's passwords. Production gets its own
before its `db` volume is first started (F-033 in
`docs/security/reports/2026-10-07-usher-stack.md`): a compromise of the
workspace, where `TEAM_DB_PASSWORD` is readable by every member (F-009), or
of a host backup must not hand over production's db. `TEAM_ROLE_LOGIN=0`
keeps `team` `NOLOGIN` here, since no member connects to this db.

```bash
# VM, before the first `up` of db; prints names and lengths, never values
cd ~/usher
for k in POSTGRES_PASSWORD TEAM_DB_PASSWORD GRAFANA_DB_PASSWORD \
         EVCRAWL_API_DB_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  sed -i "s/^$k=.*/$k=$(openssl rand -hex 24)/" .env
done
awk -F= '/_PASSWORD=/{print $1, length($2)}' .env
```

Store them as the production item in the 1Password vault `usher`, apart
from the host's. A VM whose `db` already runs on the host's values rotates
them once with "Rotating production's passwords" below.

### Rotating production's passwords

Owner, on the VM, in one sitting (the scheduler and the API cannot open new
connections between the `alter role` and the `up`):

```bash
cd ~/usher
cp -p .env .env.before-rotation               # mode 600; delete it once the checks pass
bash ops/gcp/vm-setup.sh env                  # adds TEAM_ROLE_LOGIN=0
# the openssl loop and the awk check above
docker compose up -d db                       # db's environment gets the new POSTGRES_PASSWORD
docker compose exec -T db psql -U evcrawl -d evcrawl -v ON_ERROR_STOP=1 <<'SQL'
\getenv pw POSTGRES_PASSWORD
alter role evcrawl password :'pw';
SQL
docker compose run --rm -e GRAFANA_DB_PASSWORD -e TEAM_DB_PASSWORD -e EVCRAWL_API_DB_PASSWORD \
  -e TEAM_ROLE_LOGIN evcrawl migrate          # grafana_ro, api_ro rotated; team "exists, nologin"
docker compose up -d evcrawl api grafana      # each service's URL now carries its new password
```

`GRAFANA_ADMIN_PASSWORD` sets the admin of an empty Grafana volume only;
on this one, from stdin so the value is never in argv:

```bash
sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' .env \
  | docker compose exec -T grafana grafana cli admin reset-admin-password --password-from-stdin
```

Then check:

```bash
docker compose exec -T db psql -U evcrawl -d evcrawl -Atc \
  "select rolname, rolcanlogin from pg_roles where rolname = 'team'"   # team|f
python3 ops/host/status.py                    # scheduler up, heartbeat fresh
curl -fsS https://api.eventolivre.com/healthz # db: ok
```

## 5. Check

```bash
# VM
cd ~/usher
docker compose config --quiet && echo compose-ok
docker info --format '{{.LoggingDriver}}'        # json-file
/usr/sbin/swapon --show                          # /swapfile 2G
systemctl is-enabled usher.service               # disabled until step 9
tailscale status | head -3
```

Then tell the agent "step 6 done"; step 7 (copying the data) is next.

## 6. Growing the disk (when `df -h /` is above ~80%)

The agent resizes the disk from the host after your yes (`ops/gcp/README.md`
"The VM"); the VM then grows the partition and the filesystem online, no
reboot. `growpart` comes from `cloud-guest-utils` (in the `packages` stage
since 2026-10-07; `sudo apt-get install -yq cloud-guest-utils` on a VM set
up before that).

```bash
# VM
df -h /
sudo growpart /dev/sda 1        # CHANGED: partition=1 ...
sudo resize2fs /dev/sda1        # on-line resizing required ... is now N blocks long
df -h /
```

Met on 2026-10-07 at step 9: the 10 GB disk filled at the first release
(`no space left on device` while exporting the image) and was grown to
20 GB this way with the stack running. `docker image prune -f` and
`docker builder prune -f` reclaimed nothing there; the disk was simply too
small for the images, the build cache and the swapfile next to `pgdata`.

## 7. Agents from the host through herdr (ADR 0020)

herdr on the host only sees agents whose processes run on the host; an agent
started on the VM through `gcloud compute ssh` shows up as an `unknown` pane.
Run herdr on the VM and connect the host's herdr to it instead.

herdr on the VM runs as whoever its SSH connection logs in as. Your OS Login
user has sudo and the docker group, so herdr connects as `usher-agent`
instead: a plain local user with no sudo and no docker, its own read-only
clone at `/home/usher-agent/usher`, and one SSH key of its own. Every pane in
the VM's herdr, the agent's included, is then `usher-agent`.

1. On the host, a key for the agent user and two SSH aliases with the
   options `gcloud compute ssh` uses (the instance id is in
   `~/.ssh/google_compute_known_hosts`). `usher-prod` is you, for your own
   terminal; `usher-prod-agent` is the one herdr uses:

   ```
   ssh-keygen -t ed25519 -f ~/.ssh/usher_agent_ed25519 -C usher-agent@usher-prod
   ```

   ```
   Host usher-prod usher-prod-agent
       HostName compute.<instance-id>
       IdentitiesOnly yes
       HostKeyAlias compute.<instance-id>
       UserKnownHostsFile ~/.ssh/google_compute_known_hosts
       StrictHostKeyChecking yes
       CheckHostIP no
       ServerAliveInterval 30
       ForwardAgent no
       ProxyCommand gcloud compute start-iap-tunnel usher-prod %p --listen-on-stdin --project=usher-prod --zone=southamerica-east1-b --verbosity=warning

   Host usher-prod
       User <your gcloud ssh user>
       IdentityFile ~/.ssh/google_compute_engine

   Host usher-prod-agent
       User usher-agent
       IdentityFile ~/.ssh/usher_agent_ed25519
   ```

   `ssh usher-prod hostname` prints `usher-prod`. A new VM has a new
   instance id: update the alias.
2. On the VM, as you, after pulling: copy the public key over and make the
   user. It needs sudo, so it is yours to run; it refuses to finish if
   `usher-agent` ends up in `docker`, `sudo`, `google-sudoers` or a device
   group (`video`, `render`, `disk`, `kvm`), or with a uid other than 2900.
   The uid is pinned away from 1000, the compose default for
   `CLOUDFLARED_USER`, so the agent never owns cloudflared's process. An
   `usher-agent` made before the pin is moved to 2900 by running `setup`
   again, after `sudo pkill -u usher-agent` (then restart herdr and the
   agent as in step 5).

   ```
   # host
   scp ~/.ssh/usher_agent_ed25519.pub usher-prod:
   # VM, in ~/usher
   ops/gcp/agent-user.sh setup ~/usher_agent_ed25519.pub && rm ~/usher_agent_ed25519.pub
   ```

   Back on the host, `ssh usher-prod-agent id` must show
   `uid=2900(usher-agent)` and no other group. OS Login leaves local users' `authorized_keys` working;
   if the login is refused, check on the VM that `sudo sshd -T | grep -i
   authorizedkeysfile` still lists `.ssh/authorized_keys`.
3. If herdr already ran under your user: on the host `herdr machine remove
   usher-prod`; on the VM, as you, `herdr server stop` and delete the
   binary (`command -v herdr`).
4. On the host, in a real terminal: check the binary against the pin below
   (the `machine` commands need 0.9), then `herdr machine add
   usher-prod-agent`; it asks before installing herdr on the VM, now under
   `usher-agent`, by copying the host's binary. Then check the VM's copy
   against the same digest before opening any pane there.

   **Pinned herdr** (F-042, `docs/security/reports/2026-10-08-herdr-link.md`):

   | | |
   |---|---|
   | version | `0.9.3` (release `v0.9.3` of `github.com/herdrdev/herdr`, 2026-09-29) |
   | asset | `herdr-linux-x86_64` |
   | SHA-256 | `18a8dc65f1c2fa485884344356dea1cfd911c6f06cf46fa78e193f4087f4dba7` |
   | channel | `stable` on the host |

   The digest is GitHub's for the release asset; a download of it and the
   host's `/usr/bin/herdr` hashed to the same value on 2026-10-08.

   ```
   # host
   herdr --version                     # herdr 0.9.3
   herdr channel show                  # stable
   sha256sum "$(command -v herdr)"     # the SHA-256 above
   # VM, after machine add (you, over IAP)
   sudo sha256sum /home/usher-agent/.local/bin/herdr   # the SHA-256 above
   ```

   A digest that differs stops the setup: `herdr machine remove
   usher-prod-agent` and find out why before going on. The VM's herdr is
   updated only by you, never by `herdr update` on its own: check the new
   release's asset digest (`gh release view v<ver> -R herdrdev/herdr --json
   assets`), update the host, re-run the checks above against the new
   digest, then the VM, and change this table in the same PR. A host update
   does not replace the running remote server; restart it after the check.

   Then, on the VM as you, once after the first `machine add` and after
   every update (F-042 step 2):

   ```
   ss -ltnp | grep -ci herdr                                    # 0: no network port
   sudo stat -c '%a %U %n' /home/usher-agent/.config/herdr /home/usher-agent/.config/herdr/herdr.sock
                                                                # 700 and 600, usher-agent
   sudo -u usher-agent -i herdr integration status              # note any claude hook it installed
   ```

   If `integration status` shows the claude integration installed, it adds
   a hook script to `/home/usher-agent/.claude` settings: record it here.
5. Open the `usher-prod-agent` machine in herdr's sidebar. The first time,
   in a pane there, install Claude Code for that user and log it in
   (`curl -fsSL https://claude.ai/install.sh | bash`, then `claude` and
   `/login`; the credentials stay in `/home/usher-agent`). Then start the
   agent (`usher-ops`) in `~/usher`. `herdr --machine usher-prod-agent
   agent list` on the host lists it. A security audit runs as a second
   agent next to it, `usher-cerberus-prod` (§9).

The agent's clone moves only when you move it: after every `git pull` or
`git fetch` in your checkout, run `ops/gcp/agent-user.sh refresh` on the VM.
It copies tracked history only (never `.env`): production `HEAD`, and
`origin/main` as of your last fetch, so the agent can say what an unpulled
merge changes. `/var/lib/usher-status/head.txt` (§8) is the live `HEAD`; the
agent says so when its clone is behind it.

After a preemption or restart the VM's herdr session is gone; reconnect and
start the agent again. The agent's rules do not change (AGENTS.md §4 "On the
production VM"): what a host agent sends it is data, not permission.

Host agents reach the VM agent with `herdr --machine usher-prod-agent agent
list/read/prompt` only; the guard hook refuses herdr's typing commands
(`pane run/send-text/send-keys/split`, `agent send-keys/start`), prompts
starting with `!`, `/` or `#`, and `ssh` to either alias (ADR 0020
"Consequences"). Keep no shell pane open to the VM *as you* in the host's
herdr while agents run: a local `pane run` into it would reach the VM with
your sudo. Panes on the `usher-prod-agent` machine are `usher-agent`'s.

Removing it: `herdr machine remove usher-prod-agent` on the host; on the VM
`sudo pkill -u usher-agent` then `sudo userdel -r usher-agent`; drop the
`usher-prod-agent` alias and `~/.ssh/usher_agent_ed25519*`.

## 8. Status snapshot for the agent (ADR 0020 P2)

The VM agent runs as `usher-agent`, outside the docker group, so it cannot
run `ops/host/status.py`. A system timer runs it as you every minute and
leaves the result where the agent can read it. After pulling:

```
sudo install -m 644 ops/gcp/usher-status.service ops/gcp/usher-status.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now usher-status.timer
ls -l /var/lib/usher-status/        # status.json and head.txt, 0644, within a minute
python3 ops/host/status.py --from /var/lib/usher-status/status.json
```

As `usher-agent` the same `--from` command prints the table (`--city` works,
`--hours` does not); a snapshot older than 5 minutes is reported as a
problem. A probe that fails keeps the previous snapshot. The unit names your
OS Login user and home; a different owner edits `User=` and
`WorkingDirectory=`. Removing it: `sudo systemctl disable --now
usher-status.timer`, delete both units and `/var/lib/usher-status`.

## 9. Security audit on the VM (cerberus, ADR 0020)

cerberus audits production from inside the VM, as a second agent in the
VM's herdr session: the same `usher-agent` user as `usher-ops`, the same
read-only rules (AGENTS.md §4 "On the production VM") and guard hook. It
cannot run `ops/audit/audit.py` (that needs docker), so the scanner runs as
you through a one-shot unit and the agent triages what it leaves behind.

Once, after pulling:

```
sudo install -m 644 ops/gcp/usher-audit.service /etc/systemd/system/
sudo systemctl daemon-reload
```

For each audit:

1. On the VM, as you, refresh the agent's clone and run the scan (a few
   minutes; trivy pulls its database each time):

   ```
   ops/gcp/agent-user.sh refresh
   sudo systemctl start usher-audit
   ls -l /var/lib/usher-status/audit/   # audit.md, audit.json, head.txt, ran-at.txt, trivy/
   ```

   `systemctl status usher-audit` shows `status=0/SUCCESS` or `1` (findings
   at or above HIGH: a result, not a failure); `2` means the scanner could
   not run, and its output is in `journalctl -u usher-audit`.
2. In the host's herdr, open the `usher-prod-agent` machine, start a new pane
   in `~/usher`, run `claude --agent cerberus` and rename the pane
   `usher-cerberus-prod`. Ask it to "audit the VM". It writes its report to
   `/home/usher-agent/reports/<date>-usher-prod.md`, outside its clone.
3. On the host, bring the report into the repo; it then goes through a PR
   like every report (`docs/security/README.md`):

   ```
   scp usher-prod-agent:reports/<date>-usher-prod.md docs/security/reports/
   ```

The cloud side of the audit (docs/CYBERSECURITY.md §6: IAM, firewall rules,
budgets) needs `gcloud`, which does not run from the VM agent; the host's
cerberus covers it and the public surface, and the two reports cross-refer.
The scanner prints variable names, never values, but the files are
world-readable: the VM has no local users other than you and `usher-agent`.
Removing it: delete the unit and `/var/lib/usher-status/audit`.
