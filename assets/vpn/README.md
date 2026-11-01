# VPN: VLESS over WebSocket + TLS (entry) -> foreign exit

Зачем: домашний провайдер режет 443 и fake-TLS до IP этого сервера, поэтому
вход работает на нестандартных портах с настоящим TLS (сертификат Let's Encrypt
на mon.esmien.qd.je), а Google-трафик и, при необходимости, весь трафик уходит
через внешний exit.

## Схема

    клиент -> nginx (TLS, 8443 / 2087) -> xray vpn-router (VLESS+WS, 127.0.0.1:10081)
           -> outbound "fr" (внешний exit) либо "direct" (этот сервер)

- 8443 - основной вход, 2087 - резервный (если провайдер заблокирует 8443).
- `vpn-router` - отдельный контейнер xray-core, конфиг /root/vpn-router/config.json.
- TLS терминирует nginx; xray слушает только loopback и видит обычный /ws.

## Маршрутизация

- private/geoip и bittorrent -> blocked
- Google (geosite:google + IP-диапазоны) -> fr
- по умолчанию -> fr (без этого Gemini отдаёт заглушку по стране:
  частичная маршрутизация не проходит, нужен полный туннель)
- failover: `vpn-failover.timer` каждые 2 минуты проверяет TCP до exit;
  2 неудачи подряд -> переключает default на direct, 2 успеха -> обратно на fr

## Восстановление

1. `docker run -d --name vpn-router --restart unless-stopped --user 0:0    -p 127.0.0.1:10081:10081 -v /root/vpn-router/config.json:/etc/xray/config.json:ro    ghcr.io/xtls/xray-core:latest run -c /etc/xray/config.json`
2. конфиг: `router/config.template.json` -> подставить VLESS_UUID/FR_EXIT_HOST/TROJAN_PASSWORD.
3. nginx: `nginx/vpn-8443.conf`, `nginx/vpn-2087.conf` -> /etc/nginx/conf.d/, `nginx -t && systemctl reload nginx`.
4. unit-файлы из `systemd/` -> /etc/systemd/system/, `systemctl daemon-reload && systemctl enable --now vpn-failover.timer`.
5. ссылка для клиента: `scripts/ws-link.py` (UUID читает из /root/mtproto-faketls/ws-client-uuid).

## Секреты

В репозиторий не попадают: UUID клиента, пароль внешнего exit. Живут только на хосте.
