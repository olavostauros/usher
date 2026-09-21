#!/usr/bin/env bash
# Nightly pg_dump of the evcrawl database, for running the release image on a
# host without the volume backups of ops/host/ (the owner's computer uses
# ops/host/backup-volumes.sh instead).
# Retention is not decided yet; do not change it without asking the owner.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/evcrawl}"
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
pg_dump --format=custom --no-owner "$DATABASE_URL" > "$BACKUP_DIR/evcrawl-$stamp.dump"
echo "wrote $BACKUP_DIR/evcrawl-$stamp.dump"
