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
bash ~/usher/ops/gcp/vm-setup.sh env       # DEV_SSH_BIND, CLOUDFLARED_USER, §4 limits
bash ~/usher/ops/gcp/vm-setup.sh service   # usher.service installed, not enabled
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
