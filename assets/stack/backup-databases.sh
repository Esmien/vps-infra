#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")" || exit 1

# Секреты берём из .env, не хардкодим.
ENV_FILE="$(dirname "$0")/.env"
[[ -f "$ENV_FILE" ]] || { echo "Нет $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

BACKUP_DIR="/home/ai_service/ai-stack/backups/postgres"
# Retention is by count, not by date: keep the newest RETENTION_COUNT dumps of each
# database and delete the rest, so several backups made on the same day still collapse
# to the last RETENTION_COUNT.
RETENTION_COUNT="${RETENTION_COUNT:-3}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$BACKUP_DIR/backup.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== Starting database backup ==="

export PGPASSWORD="$WEBUI_DB_PASSWORD"
log "Backing up webui_db..."
if pg_dump -h localhost -U webui -d webui_db -F c -f "$BACKUP_DIR/webui_db_${TIMESTAMP}.dump"; then
    log "✓ webui_db backed up successfully"
else
    log "✗ webui_db backup failed"
    exit 1
fi

export PGPASSWORD="$LITELLM_DB_PASSWORD"
log "Backing up litellm_db..."
if pg_dump -h localhost -U litellm -d litellm_db -F c -f "$BACKUP_DIR/litellm_db_${TIMESTAMP}.dump"; then
    log "✓ litellm_db backed up successfully"
else
    log "✗ litellm_db backup failed"
    exit 1
fi

log "Cleaning up: keeping the newest $RETENTION_COUNT dump(s) per database..."
DELETED=0
# Database names are derived from the dumps themselves (strip _YYYYMMDD_HHMMSS.dump).
mapfile -t DBS < <(
    find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -type f -printf '%f\n' \
        | sed -E 's/_[0-9]{8}_[0-9]{6}\.dump$//' | sort -u
)
for db in "${DBS[@]}"; do
    [[ -n "$db" ]] || continue
    # Newest first by mtime; %T@ is the mtime as epoch seconds.
    mapfile -t DUMPS < <(
        find "$BACKUP_DIR" -maxdepth 1 -name "${db}_*.dump" -type f -printf '%T@ %p\n' \
            | sort -rn | cut -d' ' -f2-
    )
    if (( ${#DUMPS[@]} > RETENTION_COUNT )); then
        for old in "${DUMPS[@]:RETENTION_COUNT}"; do
            if rm -f -- "$old"; then
                DELETED=$((DELETED + 1))
            fi
        done
    fi
done
log "✓ Deleted $DELETED old backup(s)"

TOTAL_BACKUPS=$(find "$BACKUP_DIR" -name "*.dump" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
log "Current status: $TOTAL_BACKUPS backup files, total size: $TOTAL_SIZE"

# Уведомление и отправка файлов выполняются отдельным таймером
# postgres-backup-notify.timer (03:00 UTC / 06:00 МСК) через monitor.py backup.

log "=== Backup completed successfully ==="