#!/bin/bash
# vps-infra-watch.sh — коммит и пуш репозитория по факту изменений.
#
# Копия логики hermes-state-watch.sh: inotify на рабочую копию, «тишина»
# QUIET секунд после последней правки (или MAXDELAY от первой), затем
# vps-infra-push.sh. Пуш сам решает, есть ли реальные изменения.
#
# Почему не systemd .path: PathModified не рекурсивен, а правки приходят
# из assets/**/SKILL.md-подобных подпапок.
set -uo pipefail

PUSH=${PUSH:-/usr/local/bin/vps-infra-push.sh}
LOG=${WATCH_LOG:-/var/log/vps-infra-autopush.log}
WATCH_DIR=${WATCH_DIR:-/root/vps-infra}
QUIET=${QUIET:-90}
MAXDELAY=${MAXDELAY:-900}
RETRY_MIN=${RETRY_MIN:-60}
RETRY_MAX=${RETRY_MAX:-300}
CYCLE=15

# .git — служебный шум; bundle/дампы/логи в репо не попадают по .gitignore,
# но события на них всё равно не должны сбрасывать debounce
EXCLUDE='(\.git/|\.git$|__pycache__/|\.tmp-|\.lock$|\.log$|bundle.*\.tar\.gz)'

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >>"$LOG"; }

retry_at=0
retry_delay=$RETRY_MIN

run_push() {
    local why="${1:-changes detected}"
    log "$why → push"
    local rc=0
    "$PUSH" >/dev/null 2>&1 || rc=$?   # push логирует сам, без дублей
    if [[ "$rc" -eq 0 ]]; then
        log "push ok"
        retry_at=0
        retry_delay=$RETRY_MIN
    else
        retry_at=$(( $(date +%s) + retry_delay ))
        log "push FAILED rc=$rc, retry in ${retry_delay}s"
        retry_delay=$(( retry_delay * 2 ))
        (( retry_delay > RETRY_MAX )) && retry_delay=$RETRY_MAX
    fi
}

[[ -x "$PUSH" ]] || { log "FATAL: $PUSH not executable"; exit 1; }
[[ -d "$WATCH_DIR" ]] || { log "FATAL: $WATCH_DIR missing"; exit 1; }
log "watcher start (quiet=${QUIET}s maxdelay=${MAXDELAY}s dir=$WATCH_DIR)"

# старт сервиса = догоняем правки, сделанные пока watcher лежал
run_push "startup catch-up"

pending=0
first=0
last=0

while true; do
    if [[ "$retry_at" -gt 0 ]] && [[ "$(date +%s)" -ge "$retry_at" ]]; then
        run_push "retry after failure"
    fi
    if ev=$(inotifywait -r -q -t "$CYCLE" \
            -e modify,create,delete,move,moved_to,moved_from,attrib,close_write \
            --format '%e %w%f' --exclude "$EXCLUDE" "$WATCH_DIR" 2>&1); then
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
                run_push
                pending=0
                first=0
            fi
        elif [[ "$rc" -ne 2 ]]; then
            log "inotifywait rc=$rc, retry in 10s"
            sleep 10
        fi
    fi
done
