#!/bin/bash
# Dead-man switch: пингует внешний сервис (healthchecks.io и совместимые), пока стек жив.
# Если хост/сеть/стек умрут — пинги прекращаются и сервис сам пришлёт алерт.
# Источник адреса: $DEADMAN_URL → /etc/deadman-ping.url → DEADMAN_URL= в notifications/.env
# Стек болен → пинг на <url>/fail (алерт сразу, не через grace).
set -uo pipefail

URL="${DEADMAN_URL:-}"
if [[ -z "$URL" && -r /etc/deadman-ping.url ]]; then
    URL=$(head -1 /etc/deadman-ping.url | tr -d '[:space:]')
fi
if [[ -z "$URL" && -r /home/ai_service/notifications/.env ]]; then
    URL=$(grep -m1 '^DEADMAN_URL=' /home/ai_service/notifications/.env | cut -d= -f2- | tr -d '[:space:]')
fi
[[ -z "$URL" ]] && exit 0   # не настроено — тихо выходим, чтобы не спамить

CONTAINERS="${DEADMAN_CONTAINERS:-litellm open-webui searxng open-terminal}"
bad=0

for c in $CONTAINERS; do
    info=$(docker inspect -f '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null)
    case "$info" in
        running\|healthy|running\|) ;;
        *) bad=1 ;;
    esac
done

systemctl is-active --quiet nginx || bad=1
systemctl is-active --quiet fail2ban || bad=1

if (( bad )); then
    curl -fsS -m 10 -o /dev/null "${URL%/}/fail" || true
    exit 1
fi
curl -fsS -m 10 -o /dev/null "$URL" || true
exit 0
