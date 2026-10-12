#!/bin/bash
# Post-boot check on the usher-prod VM (docs/DEPLOY.md §2 "Spot means
# interruptions"), run by usher.service as ExecStartPost= after `compose up -d`.
# Waits up to WAIT seconds for every service with a healthcheck to be healthy,
# then force-recreates, once, each one that is not (not running, or running but
# unhealthy). A container that came up without its compose network after a
# preemption (2026-10-08, 2026-10-11: `api` with only `lo`, Cloudflare 502)
# never exits, so `restart: unless-stopped` does not catch it; this does.
# Logs to the journal (journalctl -u usher.service). Never fails the unit.
set -uo pipefail

SERVICES=(db redis api cloudflared grafana)
WAIT=${USHER_HEAL_WAIT:-180}

state() {  # "<state> <health>" of one service, e.g. "running healthy"
  docker compose ps -a --format '{{.State}} {{.Health}}' "$1" 2>/dev/null | head -1
}

bad() {  # services not yet "running healthy"
  local s
  for s in "${SERVICES[@]}"; do
    [ "$(state "$s")" = "running healthy" ] || echo "$s"
  done
}

deadline=$((SECONDS + WAIT))
while :; do
  mapfile -t pending < <(bad)
  [ ${#pending[@]} -eq 0 ] && { echo "usher-heal: all healthy"; exit 0; }
  [ $SECONDS -ge $deadline ] && break
  sleep 10
done

for s in "${pending[@]}"; do
  echo "usher-heal: $s is '$(state "$s")' after ${WAIT}s; recreating it"
  docker compose up -d --force-recreate --no-deps "$s" || echo "usher-heal: recreate of $s failed"
done
sleep 60
mapfile -t still < <(bad)
if [ ${#still[@]} -eq 0 ]; then
  echo "usher-heal: healed ${pending[*]}"
else
  echo "usher-heal: still not healthy after one recreate: ${still[*]}; the owner looks (docs/DEPLOY.md §2)"
fi
exit 0
