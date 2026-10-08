#!/usr/bin/env bash
# Nightly snapshot of the workspace host's Docker volumes (pgdata18, homes).
# Usage: backup-volumes.sh [backup-dir]   (default: ops/host/backups)
set -euo pipefail

BACKUP_DIR=${1:-"$(cd "$(dirname "$0")" && pwd)/backups"}
KEEP_DAYS=${KEEP_DAYS:-14}
PROJECT=${COMPOSE_PROJECT_NAME:-usher}
STAMP=$(date +%F)

mkdir -p "$BACKUP_DIR"
for vol in pgdata18 homes; do
  out="$vol-$STAMP.tgz"
  docker run --rm \
    -v "${PROJECT}_${vol}":/src:ro \
    -v "$BACKUP_DIR":/dst \
    alpine:3.20 tar czf "/dst/$out" -C /src .
  echo "backup: $BACKUP_DIR/$out"
done
find "$BACKUP_DIR" -name '*.tgz' -mtime +"$KEEP_DAYS" -delete
