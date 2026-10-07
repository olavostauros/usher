#!/bin/bash
# DEPLOY.md step 6 on the usher-prod VM, run by the host owner over
#   gcloud compute ssh usher-prod --zone=southamerica-east1-b --tunnel-through-iap --project=usher-prod
# Each stage is idempotent; ops/gcp/vm-setup.md says the order and what happens
# by hand in between (the Tailscale login, the deploy key, .env, the tunnel
# credentials). Agents never run this: it installs packages, joins the tailnet
# and handles secrets.
#
# Usage: bash vm-setup.sh packages   # Docker Engine + compose, log rotation, Tailscale
#        bash vm-setup.sh key        # read-only deploy key for GitHub, prints the public half
#        bash vm-setup.sh clone      # ~/Work/usher at origin/main
#        bash vm-setup.sh env        # production values in ~/Work/usher/.env (DEPLOY.md §4)
#        bash vm-setup.sh service    # usher.service (user unit, linger) without starting it
set -euo pipefail

REPO=git@github.com:olavostauros/usher.git
CHECKOUT="$HOME/Work/usher"
KEY="$HOME/.ssh/usher_deploy"

packages() {
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  fi
  sudo apt-get update -q
  sudo apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git jq
  # Container logs stay on the VM and rotate (no Cloud Logging ingestion, DEPLOY.md §1).
  printf '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "20m", "max-file": "5"}\n}\n' \
    | sudo tee /etc/docker/daemon.json >/dev/null
  sudo systemctl restart docker
  sudo usermod -aG docker "$USER"
  if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  echo "packages: done. Log out and back in (docker group), then: sudo tailscale up --hostname=usher-prod"
}

key() {
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "usher-prod deploy (read-only)" -f "$KEY"
  if ! grep -q "usher_deploy" "$HOME/.ssh/config" 2>/dev/null; then
    printf 'Host github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$HOME/.ssh/config"
    chmod 600 "$HOME/.ssh/config"
  fi
  ssh-keyscan -t ed25519 github.com 2>/dev/null >> "$HOME/.ssh/known_hosts"
  sort -u -o "$HOME/.ssh/known_hosts" "$HOME/.ssh/known_hosts"
  echo "key: add this as a READ-ONLY deploy key (from the host: gh repo deploy-key add):"
  cat "$KEY.pub"
}

clone() {
  mkdir -p "$HOME/Work"
  [ -d "$CHECKOUT/.git" ] || git clone "$REPO" "$CHECKOUT"
  git -C "$CHECKOUT" fetch -q origin
  git -C "$CHECKOUT" checkout -q main
  git -C "$CHECKOUT" pull -q --ff-only
  git -C "$CHECKOUT" log --oneline -1
}

# Sets one KEY=value in .env, replacing an existing line.
setenv() {
  if grep -q "^$1=" "$CHECKOUT/.env"; then
    sed -i "s|^$1=.*|$1=$2|" "$CHECKOUT/.env"
  else
    echo "$1=$2" >> "$CHECKOUT/.env"
  fi
}

env() {
  [ -f "$CHECKOUT/.env" ] || { echo "env: copy the host's .env to $CHECKOUT/.env first (vm-setup.md)" >&2; exit 2; }
  chmod 600 "$CHECKOUT/.env"
  ip=$(tailscale ip -4 2>/dev/null | head -1)
  [ -n "$ip" ] || { echo "env: tailscale is not up; run sudo tailscale up first" >&2; exit 2; }
  setenv DEV_SSH_BIND "$ip"
  setenv CLOUDFLARED_USER "$(id -u):$(id -g)"
  # DEPLOY.md §4: everything but dev in 8 GB.
  setenv DB_MEMORY_LIMIT 2g
  setenv REDIS_MEMORY_LIMIT 256m
  setenv NOMINATIM_MEMORY_LIMIT 3g
  setenv NOMINATIM_SHARED_BUFFERS 512MB
  setenv NOMINATIM_MAINTENANCE_WORK_MEM 512MB
  setenv NOMINATIM_EFFECTIVE_CACHE_SIZE 2GB
  setenv NOMINATIM_SHM_SIZE 512m
  setenv EVCRAWL_MEMORY_LIMIT 1536m
  setenv API_MEMORY_LIMIT 512m
  setenv CLOUDFLARED_MEMORY_LIMIT 128m
  setenv GRAFANA_MEMORY_LIMIT 384m
  echo "env: DEV_SSH_BIND=$ip, CLOUDFLARED_USER=$(id -u):$(id -g), §4 limits set"
}

service() {
  sudo loginctl enable-linger "$USER"
  mkdir -p "$HOME/.config/systemd/user"
  cp "$CHECKOUT/ops/gcp/usher.service" "$HOME/.config/systemd/user/usher.service"
  systemctl --user daemon-reload
  systemctl --user enable usher.service
  echo "service: enabled, not started (DEPLOY.md steps 7-9 start the stack)"
}

case "${1:-}" in
  packages | key | clone | env | service) "$1" ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
