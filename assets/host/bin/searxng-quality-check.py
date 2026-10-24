#!/usr/bin/env python3
"""Watchdog качества SearXNG (не путать с healthcheck'ом контейнера).

healthcheck, docker-incident-detector и LiteLLM-watchdog зелёные даже когда
поиск отдаёт пустоту: контейнер жив, а движки молчат. Этот скрипт раз в 30
минут делает реальный поиск и проверяет, что выдача не деградировала.

Проверки:
  1. google cse (ядро выдачи) — не меньше SQ_MIN_CSE результатов на дев-запрос;
  2. общий поиск без фильтра — не меньше SQ_MIN_TOTAL результатов
     и не меньше SQ_MIN_ENGINES разных движков.
Плюс состояние контейнера searxng: если он не running — тревога сразу.

Алерт после SQ_FAILS подряд неудачных прогонов (по умолчанию 2 = ~1 час),
повтор не чаще SQ_REALERT (6 ч), плюс сообщение о восстановлении.

CLI: --no-send (только лог), --test (реальная отправка с пометкой «тест»).
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("SQ_URL", "http://127.0.0.1:8081/search")
ENV_FILE = Path("/home/ai_service/notifications/.env")
STATE_DIR = Path("/var/lib/searxng-quality")
STATE_FILE = Path(os.environ.get("SQ_STATE", str(STATE_DIR / "state.json")))
LOG_FILE = Path("/var/log/searxng-quality.log")

MIN_CSE = int(os.environ.get("SQ_MIN_CSE", "5"))
MIN_TOTAL = int(os.environ.get("SQ_MIN_TOTAL", "10"))
MIN_ENGINES = int(os.environ.get("SQ_MIN_ENGINES", "2"))
FAILS_TO_ALERT = int(os.environ.get("SQ_FAILS", "2"))
REALERT_S = int(os.environ.get("SQ_REALERT", str(6 * 3600)))

NO_SEND = "--no-send" in sys.argv
TEST = "--test" in sys.argv

# (движок или None = весь general, запрос, минимум результатов)
PROBES = (
    ("google cse", "fastapi dependency injection yield", MIN_CSE),
    (None, "как работает asyncio event loop", MIN_TOTAL),
)


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log(f"state write failed: {exc}")


def container_state() -> str:
    try:
        out = subprocess.run(
            ["docker", "inspect", "searxng", "--format",
             "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or "нет ответа"
    except Exception as exc:  # noqa: BLE001
        return f"ошибка inspect: {exc}"


def probe(engine: str | None, query: str) -> tuple[int, list[str], str]:
    params = {"q": query, "format": "json", "language": "ru"}
    if engine:
        params["engines"] = engine
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=40) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        return 0, [], f"запрос упал: {type(exc).__name__}"
    results = data.get("results") or []
    engines = sorted({r.get("engine", "?") for r in results})
    trouble = data.get("unresponsive_engines") or []
    return len(results), engines, "unresponsive: " + str(trouble) if trouble else ""


def send(text: str) -> None:
    if NO_SEND:
        log("SEND (подавлено --no-send): " + text.replace("\n", " | ")[:300])
        return
    try:
        env = {}
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
        token, owner = env.get("BOT_TOKEN"), env.get("BOT_OWNER")
        if not token or not owner:
            log("SEND failed: BOT_TOKEN/BOT_OWNER не найдены")
            return
        body = urllib.parse.urlencode(
            {"chat_id": owner, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = json.load(resp).get("ok")
        log(f"SEND ok={ok}")
    except Exception as exc:  # noqa: BLE001
        log(f"SEND failed: {type(exc).__name__}: {exc}")


def main() -> int:
    state = read_state()
    fails = int(state.get("fails", 0))
    alerted = bool(state.get("alerted", False))
    now = time.time()

    cstate = container_state()
    reasons = []
    lines = []

    if not cstate.startswith("running"):
        reasons.append(f"контейнер searxng: {cstate}")
        for engine, query, need in PROBES:
            lines.append(f"{engine or 'общий поиск'}: пропущено (контейнер лежит)")
    else:
        for engine, query, need in PROBES:
            count, engines, trouble = probe(engine, query)
            label = engine or "общий поиск"
            lines.append(
                f"{label}: {count} рез. (нужно ≥{need})"
                + (f", движки={','.join(engines)}" if engine is None else "")
                + (f", {trouble}" if trouble else "")
            )
            if engine is None:
                if count < need:
                    reasons.append(f"{label}: {count} результатов, нужно ≥{need}")
                elif len(engines) < MIN_ENGINES:
                    reasons.append(f"{label}: ответил всего {len(engines)} движок")
            elif count < need:
                reasons.append(f"{label}: {count} результатов, нужно ≥{need}")

    ok = not reasons
    log(("OK " if ok else "FAIL ") + "; ".join(lines))

    if ok:
        if alerted:
            send(
                "✅ Поиск восстановился.\n" + "\n".join(lines)
            )
            log("RECOVERY alert sent")
        state.update({"fails": 0, "alerted": False, "last_ok": now})
        write_state(state)
        return 0

    fails += 1
    state.update({"fails": fails, "last_fail": now})
    if fails >= FAILS_TO_ALERT:
        last_alert = float(state.get("last_alert") or 0)
        if not alerted or now - last_alert > REALERT_S:
            body = (
                ("🧪 ТЕСТ\n" if TEST else "⚠️ Поиск деградировал.\n\n")
                + "что не так:\n- "
                + "\n- ".join(reasons)
                + "\n\nзамеры:\n- "
                + "\n- ".join(lines)
                + f"\n\nконтейнер: {cstate}"
                + f"\nпрогонов подряд с ошибкой: {fails}"
                + "\n\nАвтоподъём не поможет — контейнер живой. Нужен человек."
            )
            send(body)
            state.update({"alerted": True, "last_alert": now})
    write_state(state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
