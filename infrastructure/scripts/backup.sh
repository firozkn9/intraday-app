#!/usr/bin/env bash
# Nightly PostgreSQL backup (docs/design/11). Run on the VM host by intraday-backup.timer.
#   infrastructure/scripts/backup.sh
# Writes $BACKUP_DIR/intraday-YYYYmmdd-HHMMSS.sql.gz, prunes old dumps, optionally copies
# off-site with rclone, and records the time in $BACKUP_DIR/last_success (System Health).
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; [ -f .env ] && . ./.env; set +a

BACKUP_DIR="${BACKUP_DIR:-/var/backups/intraday}"
RETENTION="${BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/intraday-$STAMP.sql.gz"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "${POSTGRES_DB:-intraday}" \
  --format=plain --no-owner --clean --if-exists | gzip -9 > "$OUT.tmp"
gzip -t "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
chmod 600 "$OUT"

find "$BACKUP_DIR" -name 'intraday-*.sql.gz' -mtime "+$RETENTION" -delete

if [ -n "${BACKUP_REMOTE:-}" ]; then
  rclone copy "$OUT" "$BACKUP_REMOTE" --quiet
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last_success"
echo "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
