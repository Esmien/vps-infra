#!/bin/bash
# host-junk-cleanup.sh — ежедневная уборка безопасного мусора на этом хосте.
#
# Что удаляется (всё regenerable, ни один живой сервис не затрагивается):
#   * build cache Docker старше 24 ч           (пересборка станет дольше — это ок)
#   * образы без контейнеров старше 48 ч       (свежесобранный образ не тронется)
#   * остановленные контейнеры старше 24 ч     (одноразовые migrate/job-контейнеры)
#   * dangling volumes (не подключены ни к чему)
#   * superseded-ревизии snap (active остаётся)
#   * journald — кап 100M
#   * apt cache
#   * /tmp — записи старше 3 суток (сокеты, пайпы и скрытые не трогаем)
#
# НЕ трогает: запущенные контейнеры и их образы, данные сервисов, /root/.hermes,
# бэкапы webui (у них своя ретенция), сертификаты, крон-задачи и юниты.
set -uo pipefail
umask 022

LOG=${LOG:-/var/log/host-junk-cleanup.log}
JOURNAL_MAX=${JOURNAL_MAX:-100M}
BUILD_CACHE_AGE=${BUILD_CACHE_AGE:-24h}
IMAGE_AGE=${IMAGE_AGE:-48h}
CONTAINER_AGE=${CONTAINER_AGE:-24h}
TMP_AGE_DAYS=${TMP_AGE_DAYS:-3}

mkdir -p "$(dirname "$LOG")"
# Лог не должен пухнуть сам: >2M → оставить хвост
if [[ -f "$LOG" ]] && (( $(stat -c %s "$LOG") > 2097152 )); then
    tail -c 262144 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >>"$LOG"; }
FAIL=0
step() {  # step <name> <command...>
    local name=$1; shift
    local out
    out=$("$@" 2>&1) || { FAIL=1; log "FAIL $name: ${out//$'\n'/ | }"; return 1; }
    log "ok $name: $(printf '%s' "$out" | tail -1)"
}

need_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

BEFORE_KB=$(df -P / | awk 'NR==2{print $3}')
log "=== start, used ${BEFORE_KB}K"

if need_docker; then
    step "builder-prune" docker builder prune -af --filter "until=${BUILD_CACHE_AGE}"
    step "image-prune"   docker image prune -af --filter "until=${IMAGE_AGE}"
    step "container-prune" docker container prune -f --filter "until=${CONTAINER_AGE}"
    step "volume-prune"  docker volume prune -f
else
    log "WARN docker daemon недоступен, docker-шаги пропущены"
fi

# snap: снять только superseded-ревизии; активная остаётся
if command -v snap >/dev/null 2>&1; then
    while read -r name rev; do
        [[ -z "${name:-}" ]] && continue
        step "snap-remove ${name}@${rev}" snap remove "$name" --revision="$rev"
    done < <(snap list --all 2>/dev/null | awk '/disabled/{print $1, $3}')
fi

if command -v journalctl >/dev/null 2>&1; then
    step "journal-vacuum" journalctl --vacuum-size="$JOURNAL_MAX"
fi

# apt: чистим кэш пакетов; занятый лок не ошибка (идёт unattended-upgrades)
if ! apt-get clean >/dev/null 2>&1; then
    log "WARN apt cache занят (lock), пропущено"
fi

# /tmp: только давно брошенное, без сокетов/пайпов/скрытых каталогов
TMP_FREED_BEFORE=$(du -sm /tmp 2>/dev/null | awk '{print $1}')
find /tmp -mindepth 1 -maxdepth 1 -mtime "+${TMP_AGE_DAYS}" ! -type s ! -type p ! -name '.*' \
    -exec rm -rf -- {} + 2>/dev/null || log "WARN /tmp: частичная ошибка удаления"
TMP_FREED_AFTER=$(du -sm /tmp 2>/dev/null | awk '{print $1}')
log "ok tmp-cleanup: $(( ${TMP_FREED_BEFORE:-0} - ${TMP_FREED_AFTER:-0} ))M freed"

AFTER_KB=$(df -P / | awk 'NR==2{print $3}')
log "=== done, used ${AFTER_KB}K (freed $(( (BEFORE_KB - AFTER_KB) / 1024 ))M), fail=${FAIL}"
exit "$FAIL"
