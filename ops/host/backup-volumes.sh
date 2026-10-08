#!/usr/bin/env bash
# Nightly snapshot of the workspace host's Docker volumes (pgdata18, homes).
# Usage: backup-volumes.sh [backup-dir]   (default: ops/host/backups)
set -euo pipefail

BACKUP_DIR=${1:-"$(cd "$(dirname "$0")" && pwd)/backups"}
KEEP_DAYS=${KEEP_DAYS:-14}
PROJECT=${COMPOSE_PROJECT_NAME:-usher}
STAMP=$(date +%F)

# The tarballs hold the db and every home: owner-only (F-025). The files are
# written by root inside the container, so the umask is set there too.
# alpine is pinned by its multi-arch index digest (F-002).
ALPINE=alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
for vol in pgdata18 homes; do
  out="$vol-$STAMP.tgz"
  docker run --rm \
    -v "${PROJECT}_${vol}":/src:ro \
    -v "$BACKUP_DIR":/dst \
    "$ALPINE" sh -c 'umask 077 && tar czf "/dst/$1" -C /src .' _ "$out"
  echo "backup: $BACKUP_DIR/$out"
done
find "$BACKUP_DIR" -name '*.tgz' -mtime +"$KEEP_DAYS" -delete
