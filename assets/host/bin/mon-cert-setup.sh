#!/bin/bash
# Довыпуск сертификата для mon.<domain> с ретраями (Let's Encrypt rate limit).
# Запускается таймером каждые 30 минут; при успехе ставит HTTPS-vhost,
# уведомляет в Telegram и выключает таймер.
set -uo pipefail

DOMAIN="mon.esmien.qd.je"
ACME_EMAIL="${ACME_EMAIL:-admin@ai.esmien.qd.je}"
CERT="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
TPL="/usr/local/share/mon.esmien.qd.je.ssl"
ENV_FILE="/home/ai_service/notifications/.env"

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
    exit 0
fi

if ! certbot certonly --webroot -w /var/www/html -d "$DOMAIN" --non-interactive \
        --agree-tos -m "$ACME_EMAIL" --keep-until-expiring >/tmp/mon-cert.log 2>&1; then
    echo "certbot пока не смог (рейтрай по таймеру):"
    tail -3 /tmp/mon-cert.log
    exit 1
fi

install -m 644 "$TPL" "/etc/nginx/sites-available/${DOMAIN}"
nginx -t || { echo "vhost не прошёл nginx -t"; exit 2; }
systemctl reload nginx

# APP_URL → https, чтобы ссылки в уведомлениях вели на правильный адрес
if grep -q '^APP_URL=' /home/ai_service/beszel/.env; then
    sed -i "s|^APP_URL=.*|APP_URL=https://${DOMAIN}|" /home/ai_service/beszel/.env
else
    echo "APP_URL=https://${DOMAIN}" >> /home/ai_service/beszel/.env
fi
(cd /home/ai_service/beszel && docker compose up -d beszel >/dev/null 2>&1) || true
systemctl disable --now mon-cert-setup.timer >/dev/null 2>&1 || true
notify "🟢 https://${DOMAIN} готов — мониторинг Beszel доступен снаружи"
echo "готово: https://${DOMAIN}"
