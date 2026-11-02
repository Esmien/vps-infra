#!/usr/bin/env python3
"""Напоминания об окончании VPN-подписок в Telegram (за 3, 2 и 1 день).

Читает /root/vpn-router/subs.json, для каждой ссылки берёт срок из заголовка
`subscription-userinfo` (expire=<unix>), считает остаток дней и шлёт сообщение
в Telegram через бота нотификаций. Состояние — /root/vpn-router/sub_expiry_state.json,
чтобы не повторять одно и то же сообщение дважды за день.

    sub_expiry.py            обычный прогон (по таймеру)
    sub_expiry.py --test     отправить тестовое сообщение и показать расчёт
    sub_expiry.py --force    отправить отчёт независимо от остатка дней
"""
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

BASE = Path("/root/vpn-router")
SUBS = BASE / "subs.json"
STATE = BASE / "sub_expiry_state.json"
ENV = Path("/home/ai_service/notifications/.env")
WARN_DAYS = (3, 2, 1)
UA = "Happ/2.11.0"


def read_env(*keys: str) -> dict:
    found = {}
    for line in ENV.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() in keys:
            found[k.strip()] = v.strip().strip('"').strip("'")
    return found


def expire_ts(url: str) -> int | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        info = r.headers.get("subscription-userinfo", "")
    for part in info.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "expire" and v.strip().isdigit():
            ts = int(v.strip())
            return ts if ts > 0 else None
    return None


def send(text: str) -> None:
    env = read_env("BOT_TOKEN", "BOT_OWNER")
    token, chat = env.get("BOT_TOKEN"), env.get("BOT_OWNER")
    if not token or not chat:
        raise SystemExit("нет BOT_TOKEN/BOT_OWNER в .env бота нотификаций")
    payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        if r.status != 200:
            raise SystemExit(f"telegram: HTTP {r.status}")


def main() -> int:
    test = "--test" in sys.argv
    force = "--force" in sys.argv
    subs = json.loads(SUBS.read_text())
    old_state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state = {}
    today = date.today()
    lines, msgs = [], {}

    for sub in subs:
        name = sub["name"]
        try:
            ts = expire_ts(sub["url"])
        except Exception as exc:  # noqa: BLE001 — сеть/провайдер, не повод падать
            lines.append(f"• {name}: не смог проверить ({type(exc).__name__})")
            continue
        if ts is None:
            lines.append(f"• {name}: срок не указан провайдером")
            continue
        until = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        days = (until - today).days
        lines.append(f"• {name}: до {until:%d.%m.%Y} — {days} дн.")
        key = f"{until.isoformat()}:{days}"
        already = old_state.get(name) == key and not force
        if days < 0 and not already:
            msgs[name] = f"⛔️ <b>{name}</b> истекла {until:%d.%m.%Y} ({-days} дн. назад)"
        elif (days in WARN_DAYS or force) and not already:
            left = "сегодня последний день" if days == 0 else f"осталось {days} дн."
            msgs[name] = f"⏳ <b>{name}</b> истекает {until:%d.%m.%Y} — {left}"
        state[name] = key

    to_send = list(msgs.values())

    if test:
        send("🧪 Проверка напоминаний о подписках\n" + "\n".join(lines))
        print("тестовое сообщение отправлено")
        print("\n".join(lines))
        return 0

    if to_send:
        send("\n".join(to_send) + "\n\nПродлить: @VPNUI_bot")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        print(f"отправлено: {len(to_send)}")
    else:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        print("нечего отправлять")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
