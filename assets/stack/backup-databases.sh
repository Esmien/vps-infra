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
# Lock the directory inode itself; monitor.py verify takes a shared lock on it.
exec 9<"$BACKUP_DIR"
if ! /usr/bin/flock -x -w 600 9; then
    echo "Не удалось заблокировать каталог бэкапов: $BACKUP_DIR" >&2
    exit 1
fi

# Retention is by count, not by date: keep the newest RETENTION_COUNT dumps of each
# database and delete the rest, so several backups made on the same day still collapse
# to the last RETENTION_COUNT.
RETENTION_COUNT="${RETENTION_COUNT:-3}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$BACKUP_DIR/backup.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Разделённый стек: на webui-хосте есть только webui_db, на edge — только litellm_db.
# Проверяем наличие базы ролью, которая на этом хосте точно есть: подключаемся к
# служебной базе postgres и смотрим pg_database. Нет роли или базы — пропускаем.
db_exists() {
    local db="$1" role="$2" pw="$3"
    PGPASSWORD="$pw" psql -h localhost -U "$role" -d postgres -tAc \
        "select 1 from pg_database where datname='$db'" 2>/dev/null | grep -q 1
}

backup_db() {
    local db="$1" role="$2" pw="$3"
    if ! db_exists "$db" "$role" "$pw"; then
        log "skip $db: базы/роли нет на этом хосте"
        return 0
    fi
    export PGPASSWORD="$pw"
    log "Backing up $db..."
    if pg_dump -h localhost -U "$role" -d "$db" -F c -f "$BACKUP_DIR/${db}_${TIMESTAMP}.dump"; then
        chmod 600 "$BACKUP_DIR/${db}_${TIMESTAMP}.dump"
        log "✓ $db backed up successfully"
        # Открытый дамп живёт секунды: шифруем тем же ключом, что и офсайт-копию секретов,
        # и оставляем на диске только .enc (в дампе лежат все чаты пользователей).
        if /usr/local/bin/dump-crypto encrypt "$BACKUP_DIR/${db}_${TIMESTAMP}.dump" >>"$LOG_FILE" 2>&1; then
            log "✓ $db encrypted"
        else
            log "✗ $db encryption failed — открытый дамп оставлен на месте"
            return 1
        fi
    else
        log "✗ $db backup failed"
        return 1
    fi
}

log "=== Starting database backup ==="
backup_db webui_db webui "$WEBUI_DB_PASSWORD" || exit 1
backup_db litellm_db litellm "$LITELLM_DB_PASSWORD" || exit 1

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
