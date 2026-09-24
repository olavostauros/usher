#!/usr/bin/env bash
# Release the current checkout on this host (AGENTS.md §5, host owner only):
# build the runtime image with the git SHA, migrate, smoke-crawl with --limit,
# then (re)start the scheduler, print the Grafana URL and the status. Every
# step is the documented manual one; this only runs them in order and stops
# at the first failure.
#
# Usage: ops/host/release.sh [--city <slug>] [--limit <n>] [--no-start]
#   --city      city for the smoke crawl (default: vitoria)
#   --limit     events per source in the smoke crawl (default: 10)
#   --no-start  build, migrate and smoke-crawl, but leave the scheduler alone
set -euo pipefail

cd "$(dirname "$0")/../.."

CITY=vitoria
LIMIT=10
START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --city) CITY=$2; shift 2 ;;
    --limit) LIMIT=$2; shift 2 ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "release.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -f .env ] || { echo "release.sh: no .env in $(pwd); copy .env.example first" >&2; exit 2; }

SHA=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain)" ]; then
  echo "release.sh: working tree is dirty; the image would not match $SHA" >&2
  echo "release.sh: commit or stash, then rerun" >&2
  exit 2
fi

step() { printf '\n==> %s\n' "$*"; }

step "build evcrawl:dev from $SHA"
docker compose build evcrawl --build-arg GIT_SHA="$SHA"
docker compose run --rm evcrawl version

step "migrate (one-off, before the scheduler)"
# The role passwords reach this one-off only, never the scheduler (F-026);
# compose takes each named variable's value from .env.
docker compose run --rm -e GRAFANA_DB_PASSWORD -e TEAM_DB_PASSWORD evcrawl migrate

step "smoke crawl: all sources, --city $CITY --limit $LIMIT"
# The crawl exits 1 when a source discovered events but fetched none; that
# is a release blocker, so the scheduler is not started on failure.
docker compose run --rm evcrawl crawl all --city "$CITY" --limit "$LIMIT" \
  | grep -E '"event": "crawl\.(start|done)"|"level": "(warning|error)"' || {
    echo "release.sh: smoke crawl failed; scheduler not started" >&2
    exit 1
  }

if [ "$START" = 1 ]; then
  step "start the scheduler (recreates usher-evcrawl-1 on the new image)"
  docker compose up -d evcrawl
  sleep 3
  docker compose logs --tail 3 evcrawl
fi

# The Grafana dashboards (ops/host/README.md "Dashboards") read the views the
# migrate step just (re)created, as the grafana_ro role whose password that
# step applied; they are published on DEV_SSH_BIND:3000 only. Print where they
# are when the host .env names an address. The service itself is started by
# hand (`docker compose up -d grafana`); it is not part of a release.
BIND=$(sed -n 's/^DEV_SSH_BIND=//p' .env | tail -1 | tr -d '"'"'")
if [ -n "$BIND" ]; then
  step "dashboards"
  echo "Grafana: http://$BIND:3000 (anonymous viewer on the tailnet; 'docker compose up -d grafana' if it is not running)"
fi

step "status"
exec python3 ops/host/status.py
