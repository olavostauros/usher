#!/bin/bash
# usher-prod boot script (DEPLOY.md §4, §5): Compute Engine runs it as root on
# every boot, from the instance's `startup-script` metadata. Idempotent: it only
# adds the 2 GB swap cushion the 8 GB Spot VM needs. Docker, Tailscale and the
# checkout are the owner's (DEPLOY.md step 6), never this script's.
set -euo pipefail

if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show=NAME --noheadings | grep -qx /swapfile || swapon /swapfile
sysctl -qw vm.swappiness=10
