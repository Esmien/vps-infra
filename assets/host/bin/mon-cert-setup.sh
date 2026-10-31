#!/bin/bash
# Довыпуск сертификата для mon.<domain> в общем пуле qd.je.
#
# qd.je — бесплатный домен-спейс: лимит LE «50 сертификатов на зарегистрированный
# домен за 168 ч» считается на весь qd.je и общий для всех его пользователей.
# Слоты освобождаются по скользящему окну пачками, и их разбирают другие — то есть
# это гонка. Поэтому: запоминаем retry-after от certbot, спим до (reset + 3 s) и
# затем долбим с джиттером 8–20 с до успеха, пока не кончится окно попыток.
# При успехе ставит HTTPS-vhost, переключает APP_URL, уведомляет, гасит таймер.
set -uo pipefail

DOMAIN="${DOMAIN:-mon.esmien.qd.je}"
ACME_EMAIL="${ACME_EMAIL:-admin@ai.esmien.qd.je}"
CERT="${CERT:-/etc/letsencrypt/live/${DOMAIN}/fullchain.pem}"
TPL="${TPL:-/usr/local/share/mon.esmien.qd.je.ssl}"
ENV_FILE="/home/ai_service/notifications/.env"
STATE_DIR="/var/lib/mon-cert"
RESET_FILE="${RESET_FILE:-${STATE_DIR}/reset_at}"
LOG="/tmp/mon-cert.log"

# Сколько секунд после сброса продолжаем бороться за слот в рамках одного запуска
RACE_WINDOW="${RACE_WINDOW:-150}"
# За сколько до сброса имеет смысл засыпать в этом запуске (таймер тикает раз в минуту)
SLEEP_HORIZON="${SLEEP_HORIZON:-180}"

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

# Разбор ответа certbot: 0 = выпустили, 1 = rate limit (reset записан), 2 = прочее
attempt() {
    if certbot certonly --webroot -w /var/www/html -d "$DOMAIN" --non-interactive \
            --agree-tos -m "$ACME_EMAIL" --keep-until-expiring >"$LOG" 2>&1; then
        return 0
    fi
    local ts epoch
    ts=$(grep -oE 'retry after [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC' "$LOG" \
         | head -1 | sed 's/^retry after //')
    if [[ -n "$ts" ]]; then
        epoch=$(date -u -d "$ts" +%s 2>/dev/null || echo 0)
        if [[ "$epoch" =~ ^[0-9]+$ ]] && (( epoch > 0 )); then
            echo "$epoch" > "$RESET_FILE"
            echo "429: следующий слот освободится $(date -u -d "@$epoch" '+%Y-%m-%d %H:%M:%S') UTC"
            return 1
        fi
    fi
    echo "certbot не смог, причина не rate limit:"
    tail -3 "$LOG"
    return 2
}

if [[ -f "$CERT" ]]; then
    echo "сертификат уже есть: $CERT"
    [[ "${DRY:-0}" == "1" ]] || systemctl disable --now mon-cert-setup.timer >/dev/null 2>&1 || true
    exit 0
fi

now=$(date +%s)
if [[ -f "$RESET_FILE" ]]; then
    reset=$(cat "$RESET_FILE" 2>/dev/null || echo 0)
    if [[ "$reset" =~ ^[0-9]+$ ]] && (( now < reset )); then
        wait_s=$(( reset - now + 3 ))
        if (( wait_s > SLEEP_HORIZON )); then
            echo "слот через ${wait_s}s — жду следующего тика таймера"
            exit 0
        fi
        echo "жду сброса: ${wait_s}s (reset+3s)"
        [[ "${DRY:-0}" == "1" ]] && exit 0
        sleep "$wait_s"
    fi
fi

deadline=$(( $(date +%s) + RACE_WINDOW ))
try=0
while :; do
    try=$((try + 1))
    attempt
    rc=$?
    (( rc == 0 )) && break
    (( rc == 2 )) && exit 1
    # rate limit: слот могли забрать — ждём джиттер и пробуем снова
    (( $(date +%s) >= deadline )) && { echo "окно попыток исчерпано (${try} попыток) — следующий тик таймера"; exit 1; }
    pause=$(( 8 + RANDOM % 13 ))
    echo "слот заняли, попытка ${try} через ${pause}s"
    [[ "${DRY:-0}" == "1" ]] && exit 0
    sleep "$pause"
done

install -m 644 "$TPL" "/etc/nginx/sites-available/${DOMAIN}"
nginx -t || { notify "🔴 ${DOMAIN}: vhost не прошёл nginx -t"; exit 2; }
systemctl reload nginx

if grep -q '^APP_URL=' /home/ai_service/beszel/.env; then
    sed -i "s|^APP_URL=.*|APP_URL=https://${DOMAIN}|" /home/ai_service/beszel/.env
else
    echo "APP_URL=https://${DOMAIN}" >> /home/ai_service/beszel/.env
fi
(cd /home/ai_service/beszel && docker compose up -d beszel >/dev/null 2>&1) || true

rm -f "$RESET_FILE"
systemctl disable --now mon-cert-setup.timer >/dev/null 2>&1 || true
notify "🟢 https://${DOMAIN} готов — Beszel доступен снаружи"
echo "готово: https://${DOMAIN}"
