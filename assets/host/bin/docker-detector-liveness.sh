#!/bin/bash
# Проверка, что сторожа живы (иначе тишина = ложное спокойствие):
#   1) детектор инцидентов контейнеров  — docker-autorecover
#   2) сторож LiteLLM-шлюза             — litellm-watchdog
# Оба пишут в свой state-файл каждый тик; возраст файла и активность таймера —
# единственный дешёвый признак, что они не умерли.
set -u
DOCKER_STATE=/var/lib/docker-autorecover/state.json
LITELLM_STATE=/var/lib/server-monitor/litellm_watchdog_state.json
PAUSE=/var/lib/docker-autorecover/pause
STAMP=/var/lib/docker-autorecover/liveness-alert.ts
LITELLM_STAMP=/var/lib/docker-autorecover/liveness-litellm-alert.ts
ENVF=/home/ai_service/notifications/.env
LOG=/var/log/docker-autorecover.log
[ -e "$PAUSE" ] && exit 0
now=$(date +%s)

notify() {  # notify <stamp> <log-tag> <message>
    local stamp=$1 tag=$2 msg=$3 last=0
    [ -e "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
    [ $(( now - last )) -lt 3600 ] && return 0
    echo "$now" > "$stamp"
    echo "$(date -u +%FT%TZ) LIVENESS-FAIL $tag $msg" >> "$LOG"
    set -a; . "$ENVF" 2>/dev/null; set +a
    if [ -n "${BOT_TOKEN:-}" ] && [ -n "${BOT_OWNER:-}" ]; then
        target_chat="${BOT_TARGET_CHAT:-$BOT_OWNER}"
        thread_arg=()
        [ -n "${BOT_TARGET_THREAD:-}" ] && thread_arg=(-d message_thread_id="${BOT_TARGET_THREAD}")
        curl -sS -m 15 -o /dev/null -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d chat_id="${target_chat}" "${thread_arg[@]}" --data-urlencode "text=$msg" || true
    fi
}

age_of() {  # age_of <file> -> секунды (999999 если файла нет)
    if [ -e "$1" ]; then echo $(( now - $(stat -c %Y "$1") )); else echo 999999; fi
}

# --- 1. детектор контейнеров
age=$(age_of "$DOCKER_STATE")
act=$(systemctl is-active docker-autorecover.timer 2>/dev/null)
if [ "$age" -ge 600 ] || [ "$act" != active ]; then
    notify "$STAMP" docker \
        "⚠️ Детектор инцидентов контейнеров не работает: state.json возраст ${age}s, таймер ${act}. Автоподъём сейчас не сработает — нужен человек."
fi

# --- 2. сторож LiteLLM
lage=$(age_of "$LITELLM_STATE")
lact=$(systemctl is-active litellm-watchdog.timer 2>/dev/null)
if [ "$lage" -ge 600 ] || [ "$lact" != active ]; then
    notify "$LITELLM_STAMP" litellm \
        "⚠️ Сторож LiteLLM-шлюза не работает: state.json возраст ${lage}s, таймер ${lact}. Падение шлюза пройдёт незамеченным — нужен человек."
fi

exit 0
