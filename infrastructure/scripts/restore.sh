#!/usr/bin/env bash
# Restore a backup.
#   infrastructure/scripts/restore.sh --test  [dump.sql.gz]   # restore into a scratch DB and verify
#   infrastructure/scripts/restore.sh --apply  dump.sql.gz    # replace the live DB (engine stopped)
# --test is safe to run any time (the restore test of Phase 13). --apply asks for confirmation.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; [ -f .env ] && . ./.env; set +a

MODE="${1:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/intraday}"
DUMP="${2:-$(ls -1t "$BACKUP_DIR"/intraday-*.sql.gz 2>/dev/null | head -1)}"
[ -f "$DUMP" ] || { echo "no dump found"; exit 1; }
PSQL=(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER")

case "$MODE" in
  --test)
    DB="intraday_restore_test"
    "${PSQL[@]}" -d postgres -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
    gunzip -c "$DUMP" | "${PSQL[@]}" -q -d "$DB" > /dev/null
    for t in alembic_version settings_versions users positions orders daily_pnl; do
      n=$("${PSQL[@]}" -d "$DB" -tAc "SELECT count(*) FROM $t")
      echo "$t: $n rows"
    done
    "${PSQL[@]}" -d postgres -c "DROP DATABASE $DB"
    echo "restore test ok: $DUMP"
    ;;
  --apply)
    read -r -p "Replace the LIVE database with $DUMP? Type RESTORE: " answer
    [ "$answer" = "RESTORE" ] || { echo "aborted"; exit 1; }
    docker compose stop engine api
    gunzip -c "$DUMP" | "${PSQL[@]}" -q -d "${POSTGRES_DB:-intraday}" > /dev/null
    docker compose up -d api engine
    echo "restored; the engine reconciles with the broker before any trade (RECONCILING)"
    ;;
  *) echo "usage: $0 --test [dump] | --apply dump"; exit 2 ;;
esac
