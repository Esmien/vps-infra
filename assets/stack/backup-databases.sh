#!/bin/bash
# Ежедневный бэкап баз AI-стека: pg_dump -Fc -> шифрование dump-crypto -> retention.
#
# Скрипт один и тот же на обоих хостах разделённого стека: он сам определяет,
# какие базы доступны локально, и пропускает отсутствующие (на RU есть только
# webui_db, на зарубежном — только litellm_db). BACKUP_DBS может сузить список.
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

mkdir -p "$BACKUP_DIR"

log "=== Starting database backup ==="

# dump_db <db> <role> <password>
# Пропускает базу, которой нет на этом хосте (разделённый стек), и падает только
# на реальной ошибке pg_dump/шифрования.
dump_db() {
    local db="$1" role="$2" pass="$3"
    export PGPASSWORD="$pass"
    if ! psql -h localhost -U "$role" -d "$db" -tAc 'select 1' >/dev/null 2>&1; then
        log "skip $db: роли/базы нет на этом хосте"
        return 0
    fi
    log "Backing up $db..."
    if ! pg_dump -h localhost -U "$role" -d "$db" -F c -f "$BACKUP_DIR/${db}_${TIMESTAMP}.dump"; then
        log "✗ $db backup failed"
        exit 1
    fi
    chmod 600 "$BACKUP_DIR/${db}_${TIMESTAMP}.dump"
    log "✓ $db backed up successfully"
    # Открытый дамп живёт секунды: шифруем тем же ключом, что и офсайт-копию секретов,
    # и оставляем на диске только .enc (в дампе лежат все чаты пользователей).
    if /usr/local/bin/dump-crypto encrypt "$BACKUP_DIR/${db}_${TIMESTAMP}.dump" >>"$LOG_FILE" 2>&1; then
        log "✓ $db encrypted"
    else
        log "✗ $db encryption failed — открытый дамп оставлен на месте"
        exit 1
    fi
}

for pair in "webui_db:webui:WEBUI_DB_PASSWORD" "litellm_db:litellm:LITELLM_DB_PASSWORD"; do
    IFS=: read -r db role var <<<"$pair"
    if [[ -n "${BACKUP_DBS:-}" && " $BACKUP_DBS " != *" $db "* ]]; then
        log "skip $db: не входит в BACKUP_DBS"
        continue
    fi
    # shellcheck disable=SC2154  # переменные приходят из .env через source
    dump_db "$db" "$role" "${!var}"
done

log "Cleaning up: keeping the newest $RETENTION_COUNT dump(s) per database..."
DELETED=0
# Database names are derived from the dumps themselves (strip _YYYYMMDD_HHMMSS.dump.enc).
mapfile -t DBS < <(
    find "$BACKUP_DIR" -maxdepth 1 -name '*.dump.enc' -type f -printf '%f\n' \
        | sed -E 's/_[0-9]{8}_[0-9]{6}\.dump\.enc$//' | sort -u
)
for db in "${DBS[@]}"; do
    [[ -n "$db" ]] || continue
    # Newest first by mtime; %T@ is the mtime as epoch seconds.
    mapfile -t DUMPS < <(
        find "$BACKUP_DIR" -maxdepth 1 -name "${db}_*.dump.enc" -type f -printf '%T@ %p\n' \
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

TOTAL_BACKUPS=$(find "$BACKUP_DIR" -maxdepth 1 -name "*.dump.enc" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
log "Current status: $TOTAL_BACKUPS backup files, total size: $TOTAL_SIZE"

# Уведомление и отправка файлов выполняются отдельным таймером
# postgres-backup-notify.timer (03:00 UTC / 06:00 МСК) через monitor.py backup.

log "=== Backup completed successfully ==="
