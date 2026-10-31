#!/bin/bash
# Довыпуск сертификата для mon.<domain> с точным заходом на сброс лимита LE.
# 429 = «50 сертификатов на зарегистрированный домен за 168 ч»; certbot отдаёт
# в ответе retry after <UTC>. Скрипт запоминает это время и в следующий запуск
# спит до (reset + 4 s), затем пробует. При успехе ставит HTTPS-vhost,
# переключает APP_URL Beszel на https, уведомляет и выключает таймер.
set -uo pipefail

DOMAIN="mon.esmien.qd.je"
ACME_EMAIL="${ACME_EMAIL:-admin@ai.esmien.qd.je}"
CERT="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
TPL="/usr/local/share/mon.esmien.qd.je.ssl"
ENV_FILE="/home/ai_service/notifications/.env"
STATE_DIR="/var/lib/mon-cert"
RESET_FILE="${STATE_DIR}/reset_at"
LOG="/tmp/mon-cert.log"

mkdir -p "$STATE_DIR"

notify() {
    local text="$1"
    [[ -r "$ENV_FILE" ]] || return 0
    local token
    token=$(grep -m1 '^BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    [[ -n "$token" ]] || return 0
    curl -fsS -m 15 -o /dev/null -X POST "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id=214062001 --data-urlencode "text=${text}" || true
}

if [[ -f "$CERT" ]]; then
    echo "сертификат уже есть: $CERT"
    [[ "${DRY:-0}" == "1" ]] || systemctl disable --now mon-cert-setup.timer >/dev/null 2>&1 || true
    exit 0
fi

# Если известен момент сброса — не дёргаем LE заранее, а ждём reset+4s.
now=$(date +%s)
if [[ -f "$RESET_FILE" ]]; then
    reset=$(cat "$RESET_FILE" 2>/dev/null || echo 0)
    if [[ "$reset" =~ ^[0-9]+$ ]] && (( now < reset )); then
        wait_s=$(( reset - now + 4 ))
        if (( wait_s > 420 )); then
            echo "лимит снимется через ${wait_s}s — жду следующего тика таймера"
            exit 0
        fi
        echo "жду сброса лимита: ${wait_s}s (reset+4s)"
        [[ "${DRY:-0}" == "1" ]] && exit 0
        sleep "$wait_s"
    fi
fi

if ! certbot certonly --webroot -w /var/www/html -d "$DOMAIN" --non-interactive \
        --agree-tos -m "$ACME_EMAIL" --keep-until-expiring >"$LOG" 2>&1; then
    ts=$(grep -oE 'retry after [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC' "$LOG" \
         | head -1 | sed 's/^retry after //')
    if [[ -n "$ts" ]]; then
        epoch=$(date -u -d "$ts" +%s 2>/dev/null || echo 0)
        if [[ "$epoch" =~ ^[0-9]+$ ]] && (( epoch > 0 )); then
            echo "$epoch" > "$RESET_FILE"
            echo "429: следующий заход на $(date -u -d "@$((epoch + 4))" '+%Y-%m-%d %H:%M:%S') UTC"
        fi
    else
        echo "certbot не смог, причина не rate limit:"
        tail -3 "$LOG"
    fi
    exit 1
fi

install -m 644 "$TPL" "/etc/nginx/sites-available/${DOMAIN}"
nginx -t || { notify "🔴 mon: vhost не прошёл nginx -t"; exit 2; }
systemctl reload nginx

if grep -q '^APP_URL=' /home/ai_service/beszel/.env; then
    sed -i "s|^APP_URL=.*|APP_URL=https://${DOMAIN}|" /home/ai_service/beszel/.env
else
    echo "APP_URL=https://${DOMAIN}" >> /home/ai_service/beszel/.env
fi
(cd /home/ai_service/beszel && docker compose up -d beszel >/dev/null 2>&1) || true

rm -f "$RESET_FILE"
systemctl disable --now mon-cert-setup.timer >/dev/null 2>&1 || true
notify "🟢 https://${DOMAIN} готов — мониторинг Beszel доступен снаружи"
echo "готово: https://${DOMAIN}"
