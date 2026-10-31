#!/bin/bash
# Анти-сон для GitHub Actions: в публичных репозиториях scheduled-воркфлоу
# автоматически отключаются после 60 дней без активности. Раз в месяц
# дописываем строку в файл репо — watcher делает коммит, репо остаётся живым.
set -euo pipefail
F=/root/vps-infra/maintainer/uptime-heartbeat.txt
[[ -d /root/vps-infra ]] || exit 0
echo "$(date -u +%Y-%m-%dT%H:%MZ) alive" >> "$F"
tail -n 24 "$F" > "$F.tmp" && mv "$F.tmp" "$F"
