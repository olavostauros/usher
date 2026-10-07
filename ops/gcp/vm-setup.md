# usher-prod VM setup (DEPLOY.md step 6)

Owner only, by hand: this installs packages, joins the tailnet and places
secrets. Nothing here starts the stack; steps 7-9 do. Run the commands in
order. "host" is your computer (`~/Work/usher`), "VM" is a shell on
`usher-prod`.

## 1. Copy the script to the VM and open a shell

```bash
# host
gcloud compute scp ops/gcp/vm-setup.sh usher-prod:~ \
  --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
gcloud compute ssh usher-prod --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
```

## 2. Packages, then the tailnet

```bash
# VM
bash vm-setup.sh packages
exit            # log out so the docker group applies; ssh back in (step 1, second line)
sudo tailscale up --hostname=usher-prod   # opens a login URL: approve it in your browser
tailscale ip -4                           # the VM's tailnet address
```

## 3. Deploy key and checkout

```bash
# VM
bash vm-setup.sh key          # prints an ssh-ed25519 public key
```

```bash
# host: paste the key into a file and add it READ-ONLY (no --allow-write)
gh repo deploy-key add /path/to/usher_deploy.pub --repo olavostauros/usher --title usher-prod
```

```bash
# VM
bash vm-setup.sh clone        # ~/Work/usher at main, prints the commit
```

## 4. Secrets

`.env` and the tunnel credentials go host → VM directly; they never pass
through an agent, a commit or a chat.

```bash
# host
gcloud compute scp .env usher-prod:~/Work/usher/.env \
  --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
gcloud compute scp ops/host/cloudflared/credentials.json \
  usher-prod:~/Work/usher/ops/host/cloudflared/credentials.json \
  --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
```

```bash
# VM
chmod 600 ~/Work/usher/ops/host/cloudflared/credentials.json
bash ~/Work/usher/ops/gcp/vm-setup.sh env       # DEV_SSH_BIND, CLOUDFLARED_USER, §4 limits
bash ~/Work/usher/ops/gcp/vm-setup.sh service   # usher.service enabled, not started
```

## 5. Check

```bash
# VM
cd ~/Work/usher
docker compose config --quiet && echo compose-ok
docker info --format '{{.LoggingDriver}}'        # json-file
swapon --show                                    # /swapfile 2G
systemctl --user is-enabled usher.service        # enabled
tailscale status | head -3
```

Then tell the agent "step 6 done"; step 7 (copying the data) is next.
