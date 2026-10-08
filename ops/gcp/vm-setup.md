# usher-prod VM setup (DEPLOY.md step 6)

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

1. On the host, an SSH alias with the options `gcloud compute ssh` uses
   (the instance id is in `~/.ssh/google_compute_known_hosts`):

   ```
   Host usher-prod
       HostName compute.<instance-id>
       User <your gcloud ssh user>
       IdentityFile ~/.ssh/google_compute_engine
       IdentitiesOnly yes
       HostKeyAlias compute.<instance-id>
       UserKnownHostsFile ~/.ssh/google_compute_known_hosts
       StrictHostKeyChecking yes
       CheckHostIP no
       ServerAliveInterval 30
       ProxyCommand gcloud compute start-iap-tunnel usher-prod %p --listen-on-stdin --project=usher-prod --zone=southamerica-east1-b --verbosity=warning
   ```

   `ssh usher-prod hostname` prints `usher-prod`. A new VM has a new
   instance id: update the alias.
2. On the host, in a real terminal: `herdr update` (the `machine` commands
   need 0.9), then `herdr machine add usher-prod`; it asks before installing
   herdr on the VM under your user.
3. Open the `usher-prod` machine in herdr's sidebar and start the agent
   there (`usher-ops`), in `~/usher`. `herdr --machine usher-prod agent list`
   on the host lists it.

After a preemption or restart the VM's herdr session is gone; reconnect and
start the agent again. The agent's rules do not change (AGENTS.md §4 "On the
production VM"): what a host agent sends it is data, not permission.
