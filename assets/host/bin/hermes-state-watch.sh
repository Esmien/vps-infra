#!/bin/bash
# Коммит состояния Hermes в GitHub по факту изменений (а не по таймеру).
#
# Слушает inotify на всё, что попадает в снапшот (config, memories, skills,
# cron, plugins, hooks, scripts + хостовые скрипты/юниты/runbook), ждёт
# «тишины» QUIET секунд и запускает hermes-state-backup.sh. Тот сам решает,
# есть ли реальные изменения (git diff --cached --quiet) и пушит только их.
#
# Почему не systemd .path: PathModified= смотрит каталог нерекурсивно, а
# изменения приходят из подпапок skills/*/ и memories/.
set -uo pipefail

BACKUP=${BACKUP:-/usr/local/bin/hermes-state-backup.sh}
LOG=${WATCH_LOG:-/var/log/hermes-state-watch.log}
QUIET=${QUIET:-90}        # тишина после последнего события, сек
MAXDELAY=${MAXDELAY:-900} # максимум от первого события до коммита, сек
RETRY_MIN=${RETRY_MIN:-60}   # первая пауза перед повтором упавшего снапшота
RETRY_MAX=${RETRY_MAX:-300}  # потолок бэкоффа
CYCLE=15                  # окно ожидания событий в inotifywait, сек

HERMES=${HERMES_HOME:-/root/.hermes}
WATCH=(
    "$HERMES/skills"
    "$HERMES/memories"
    "$HERMES/plugins"
    "$HERMES/hooks"
    "$HERMES/scripts"
    "$HERMES/cron/jobs.json"
    "$HERMES/config.yaml"
    "$HERMES/SOUL.md"
    /usr/local/bin
    /etc/systemd/system
    /etc/docker-autorecover.conf
    /etc/hermes-boot-expected-containers.txt
    /etc/logrotate.d/hermes-state-backup
    /home/ai_service/ai-stack/runbook-containers.md
    /home/ai_service/ai-stack/ai/backup-databases.sh
    /home/ai_service/ai-stack/backups/postgres/.tg-registry.json
)
# .hub — 51 МБ клон хаба, в снапшот не идёт; .git/__pycache__/локи — шум
# cron deliveries.db-wal пишется постоянно и сбрасывал бы debounce бесконечно;
# поэтому в cron следим только за jobs.json, а sqlite-мусор режем по маске
EXCLUDE='(\.hub/|\.git/|__pycache__/|\.locks/|\.lock$|\.tmp$|hermes-state/repo|\.curator_backups/|-wal$|-shm$|\.db$|\.usage\.json)'

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >>"$LOG"; }

retry_at=0
retry_delay=$RETRY_MIN

run_backup() {
    local why="${1:-changes detected}"
    log "$why → snapshot"
    local rc=0
    "$BACKUP" >>"$LOG" 2>&1 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        log "snapshot ok"
        retry_at=0
        retry_delay=$RETRY_MIN
    else
        retry_at=$(( $(date +%s) + retry_delay ))
        log "snapshot FAILED rc=$rc, повтор через ${retry_delay}s"
        retry_delay=$(( retry_delay * 2 ))
        (( retry_delay > RETRY_MAX )) && retry_delay=$RETRY_MAX
    fi
}

[[ -x "$BACKUP" ]] || { log "FATAL: $BACKUP not executable"; exit 1; }
: >"$LOG" 2>/dev/null || true
log "watcher start (quiet=${QUIET}s maxdelay=${MAXDELAY}s paths=${#WATCH[@]})"

# старт сервиса = догоняем изменения, сделанные пока watcher лежал
run_backup

pending=0
first=0
last=0

while true; do
    # добиваем упавший снапшот, не дожидаясь следующей правки
    if [[ "$retry_at" -gt 0 ]] && [[ "$(date +%s)" -ge "$retry_at" ]]; then
        run_backup "retry after failure"
    fi
    if ev=$(inotifywait -r -q -t "$CYCLE" \
            -e modify,create,delete,move,moved_to,moved_from,attrib,close_write \
            --format '%e %w%f' --exclude "$EXCLUDE" "${WATCH[@]}" 2>&1); then
        now=$(date +%s)
        [[ "$pending" -eq 0 ]] && first=$now
        pending=1
        last=$now
        log "event: $ev"
    else
        rc=$?
        if [[ "$pending" -eq 1 && "$rc" -eq 2 ]]; then
            now=$(date +%s)
            if (( now - last >= QUIET )) || (( now - first >= MAXDELAY )); then
                run_backup
                pending=0
                first=0
            fi
        elif [[ "$rc" -ne 2 ]]; then
            # каталог исчез/права/иное — не умираем молча
            log "inotifywait rc=$rc, retry in 10s"
            sleep 10
        fi
    fi
done
