#!/usr/bin/env python3
"""Watchdog independentnosti LiteLLM-шлюза.

Ловит ровно ту дыру, которую не закрывают события Docker: контейнер `Up`/`healthy`,
но трафик не обслуживается (зависший процесс, выбитые ключи группы, лежащий upstream),
а также всплеск отказов по логам LiteLLM.

Запускается systemd-таймером раз в минуту. Алерт отправляется напрямую в Telegram
(не через LiteLLM и не через Hermes), поэтому сообщение уйдёт даже когда шлюз мёртв.

Состояние: /var/lib/server-monitor/litellm_watchdog_state.json
Лог:       /var/log/litellm-watchdog.log

Флаги:
  --test      отправить тестовое сообщение (проверка доставки)
  --no-send   не отправлять в Telegram, только напечатать тело алерта (для проверки логики)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path("/home/ai_service/notifications/.env")
STATE_FILE = Path("/var/lib/server-monitor/litellm_watchdog_state.json")
LOG_FILE = Path("/var/log/litellm-watchdog.log")
LIVENESS_URL = os.environ.get("LITELLM_WATCHDOG_URL", "http://127.0.0.1:4000/health/liveliness")
CONTAINER = "litellm"

HTTP_TIMEOUT = 5
FAIL_THRESHOLD = 2          # столько подряд неудачных проб => DOWN
ERR_WINDOW_MIN = 15         # окно анализа отказов
ERR_MIN_CALLS = 10          # меньше — не судим
ERR_RATIO = 0.5             # доля отказов, при которой тревога
ERR_ALERT_COOLDOWN = 1800   # не чаще раза в 30 минут


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {msg}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"state write failed: {exc}")


def send(text: str, no_send: bool) -> None:
    if no_send:
        print(text)
        return
    token = os.environ.get("BOT_TOKEN", "")
    chat = os.environ.get("BOT_OWNER", "")
    if not token or not chat:
        log("BOT_TOKEN/BOT_OWNER missing — alert not sent")
        return
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
            log(f"alert sent ok={payload.get('ok')} message_id={payload.get('result', {}).get('message_id')}")
    except (urllib.error.URLError, ValueError) as exc:
        log(f"alert send failed: {exc}")


def probe_http() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(LIVENESS_URL, timeout=HTTP_TIMEOUT) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - сообщаем текст любой ошибки
        return False, type(exc).__name__


def probe_container() -> str:
    try:
        out = subprocess.run(
            ["docker", "inspect", CONTAINER, "--format",
             "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"inspect failed: {exc}"


def probe_error_rate() -> tuple[bool, str]:
    """Доля отказов за окно, без битых /v1/models и pass-through (они пишутся в спенд-логи)."""
    sql = (
        "select count(*), count(*) filter (where status <> 'success') "
        "from \"LiteLLM_SpendLogs\" where \"startTime\" > now() - interval '%d minutes' "
        "and call_type not in ('/v1/models','pass_through_endpoint');" % ERR_WINDOW_MIN
    )
    try:
        out = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", "litellm_db", "-tAc", sql],
            capture_output=True, text=True, timeout=30,
        )
        total, bad = (int(x) for x in out.stdout.strip().split("|"))
    except Exception as exc:  # noqa: BLE001
        log(f"error-rate probe failed: {exc}")
        return False, ""
    if total < ERR_MIN_CALLS:
        return False, ""
    ratio = bad / total
    if ratio >= ERR_RATIO:
        return True, f"{bad} из {total} вызовов с ошибкой за {ERR_WINDOW_MIN} мин ({ratio:.0%})"
    return False, ""


def main() -> int:
    load_env()
    no_send = "--no-send" in sys.argv

    if "--test" in sys.argv:
        send("🛡 <b>LiteLLM watchdog</b>: тест доставки. Шлюз под наблюдением.", no_send)
        return 0

    state = read_state()
    fails = int(state.get("fails", 0))
    down = bool(state.get("down", False))
    now = int(time.time())

    ok, detail = probe_http()
    container = probe_container()

    if ok:
        fails = 0
        if down:
            text = (
                "✅ <b>LiteLLM снова отвечает</b>\n"
                f"Проба: {detail}\nКонтейнер: <code>{container}</code>"
            )
            send(text, no_send)
            log("recovery alert")
        down = False
    else:
        fails += 1
        if fails >= FAIL_THRESHOLD and not down:
            text = (
                "🔴 <b>LiteLLM не отвечает</b>\n"
                f"{fails} неудачных проб подряд ({detail})\n"
                f"Контейнер: <code>{container}</code>\n"
                f"URL: <code>{LIVENESS_URL}</code>\n"
                "Проверить: docker logs --tail 50 litellm"
            )
            send(text, no_send)
            log(f"down alert: {detail} container={container}")
            down = True

    alarm, err_detail = probe_error_rate()
    if alarm:
        last = int(state.get("last_err_alert", 0))
        if now - last >= ERR_ALERT_COOLDOWN:
            send(
                "⚠️ <b>LiteLLM: всплеск отказов</b>\n"
                f"{err_detail}\n"
                f"Контейнер: <code>{container}</code>\n"
                "Шлюз жив, но вызовы моделей падают — смотреть ключи группы и провайдера.",
                no_send,
            )
            log(f"error-rate alert: {err_detail}")
            state["last_err_alert"] = now

    state.update({"fails": fails, "down": down, "last_probe": now, "last_detail": detail})
    write_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
