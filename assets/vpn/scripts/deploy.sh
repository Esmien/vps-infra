#!/usr/bin/env bash
# deploy.sh — разворачивает VPN-стек Esmien на чистом Ubuntu 22.04/24.04.
#
#   bash deploy.sh vpn.example.com [email@example.com]
#
# Что делает (идемпотентно, можно запускать повторно):
#   1. ставит nginx + stream-модуль + certbot;
#   2. раскладывает скрипты маршрутизатора в /root/vpn-router;
#   3. генерирует конфиг Xray из config.template.json (со своим UUID);
#   4. выпускает сертификат Let's Encrypt на домен;
#   5. поднимает контейнер vpn-router (network host) и рендерит nginx-входы;
#   6. печатает ссылку на подписку.
#
# Внешние выходы (fr/fi) после развёртывания добавляются отдельно:
#   python3 /root/vpn-router/import_sub.py <файл-подписки> [индексы мёртвых узлов]
set -euo pipefail

DOMAIN="${1:?usage: deploy.sh <domain> [email]}"
EMAIL="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS="$(cd "$HERE/.." && pwd)"          # assets/vpn
ROUTER=/root/vpn-router
WEB=/var/www/esmien-sub
UUID_FILE=/root/mtproto-faketls/ws-client-uuid

log() { printf '\n== %s\n' "$*"; }

log "зависимости"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx libnginx-mod-stream certbot python3-certbot-nginx python3-jq curl openssl >/dev/null

log "каталоги и скрипты"
mkdir -p "$ROUTER" "$WEB" /var/www/html
install -m 755 "$ASSETS"/router/*.py "$ROUTER"/
[ -f "$ROUTER/brand.json" ] || install -m 644 "$ASSETS"/router/brand.json "$ROUTER"/brand.json
mkdir -p /root/mtproto-faketls
[ -f "$UUID_FILE" ] || openssl rand -hex 16 > "$UUID_FILE"
chmod 600 "$UUID_FILE"
UUID="$(cat "$UUID_FILE")"

log "brand.json -> $DOMAIN"
python3 - "$ROUTER/brand.json" "$DOMAIN" <<'PY'
import json, sys
p, host = sys.argv[1], sys.argv[2]
b = json.load(open(p))
b["host"] = host
b["cert_dir"] = f"/etc/letsencrypt/live/{host}"
json.dump(b, open(p, "w"), ensure_ascii=False, indent=2)
PY

log "конфиг Xray"
if [ ! -f "$ROUTER/config.json" ]; then
  python3 - "$ASSETS/router/config.template.json" "$ROUTER/config.json" "$UUID" <<'PY'
import json, sys
src, dst, uid = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src).read().replace("<VLESS_UUID>", uid)
cfg = json.loads(text)
# внешние выходы подключаются позже через import_sub.py — пока без них
cfg["outbounds"] = [o for o in cfg["outbounds"] if o["tag"] in ("direct", "blocked")]
json.dump(cfg, open(dst, "w"), ensure_ascii=False, indent=2)
print("config.json создан:", len(cfg["inbounds"]), "inbounds")
PY
  chmod 600 "$ROUTER/config.json"
else
  echo "config.json уже есть — не трогаю"
fi

log "сертификат"
cat > /etc/nginx/conf.d/99-acme-"$DOMAIN".conf <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 404; }
}
EOF
nginx -t && systemctl reload nginx
CERTBOT_ARGS=(certonly --webroot -w /var/www/html -d "$DOMAIN" --keep-until-expiring --non-interactive --agree-tos -m "${EMAIL:-admin@$DOMAIN}")
certbot "${CERTBOT_ARGS[@]}" || {
  echo "certbot не смог выпустить сертификат — проверь DNS и лимиты Let's Encrypt"
  echo "повторить позже: certbot ${CERTBOT_ARGS[*]}"
}
[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] || { echo "нет сертификата, дальше идти нельзя"; exit 1; }

log "nginx-входы"
python3 "$ROUTER/apply_brand.py"
nginx -t && systemctl reload nginx

log "контейнер vpn-router"
docker rm -f vpn-router >/dev/null 2>&1 || true
docker run -d --name vpn-router --restart unless-stopped --network host \
  -v "$ROUTER/config.json":/etc/xray/config.json:ro \
  ghcr.io/xtls/xray-core:latest run -c /etc/xray/config.json >/dev/null
sleep 5
docker ps --filter name=vpn-router --format '  {{.Names}}: {{.Status}}'

log "подписка"
python3 "$ROUTER/make_sub.py" --new || python3 "$ROUTER/make_sub.py"

log "проверка"
for p in /general /france /fi; do
  printf '  %-9s %s\n' "$p" "$(curl -sS -m 8 --resolve "$DOMAIN:8443:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$DOMAIN:8443$p" || true)"
done
echo "400 = вход ждёт WebSocket, это норма."
echo
echo "дальше: python3 $ROUTER/import_sub.py <sub-файл>   # подключить внешние выходы"
