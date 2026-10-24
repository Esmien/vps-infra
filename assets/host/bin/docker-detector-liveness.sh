#!/bin/bash
# Проверка, что сам детектор инцидентов жив (иначе тишина = ложное спокойствие).
set -u
STATE=/var/lib/docker-autorecover/state.json
PAUSE=/var/lib/docker-autorecover/pause
STAMP=/var/lib/docker-autorecover/liveness-alert.ts
ENVF=/home/ai_service/notifications/.env
LOG=/var/log/docker-autorecover.log
[ -e "$PAUSE" ] && exit 0
now=$(date +%s)
age=999999
[ -e "$STATE" ] && age=$(( now - $(stat -c %Y "$STATE") ))
act=$(systemctl is-active docker-autorecover.timer 2>/dev/null)
if [ "$age" -lt 600 ] && [ "$act" = active ]; then exit 0; fi
last=0; [ -e "$STAMP" ] && last=$(cat "$STAMP" 2>/dev/null || echo 0)
[ $(( now - last )) -lt 3600 ] && exit 0
echo "$now" > "$STAMP"
msg="⚠️ Детектор инцидентов контейнеров не работает: state.json возраст ${age}s, таймер ${act}. Автоподъём сейчас не сработает — нужен человек."
echo "$(date -u +%FT%TZ) LIVENESS-FAIL $msg" >> "$LOG"
set -a; . "$ENVF" 2>/dev/null; set +a
curl -sS -m 15 -o /dev/null -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" -d chat_id="${BOT_OWNER}" --data-urlencode "text=$msg" || true
