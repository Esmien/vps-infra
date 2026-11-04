#!/usr/bin/env python3
"""Watchdog независимости LiteLLM-шлюза.

Ловит ровно ту дыру, которую не закрывают события Docker: контейнер `Up`/`healthy`,
но трафик не обслуживается (зависший процесс, выбитые ключи группы, лежащий upstream),
а также всплеск отказов по логам LiteLLM.

Запускается systemd-таймером раз в минуту. Алерт отправляется напрямую в Telegram
(не через LiteLLM и не через Hermes), поэтому сообщение уйдёт даже когда шлюз мёртв.
Любая настоящая тревога дополнительно поднимает Hermes-агента на разбор
(`litellm-incident-agent.sh`, ранбук — skill `litellm-fallout-triage`): он кладёт
отчёт владельцу в Telegram, а не только факт «что-то упало».

Состояние: /var/lib/server-monitor/litellm_watchdog_state.json
Лог:       /var/log/litellm-watchdog.log
Улики:     /var/lib/litellm-incidents/<ts>-<reason>/

Флаги:
  --test      отправить тестовое сообщение (проверка доставки)
  --no-send   не отправлять в Telegram, только напечатать тело алерта (для проверки логики)
  --drill     учебная тревога: полный путь (улики + пробуждение агента), текст помечен DRILL
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
INCIDENTS = Path(os.environ.get("LITELLM_INCIDENTS", "/var/lib/litellm-incidents"))
AGENT = os.environ.get("LITELLM_AGENT", "/usr/local/bin/litellm-incident-agent.sh")
PAUSE_FILE = Path(os.environ.get("LITELLM_PAUSE", "/var/lib/docker-autorecover/pause"))
LIVENESS_URL = os.environ.get("LITELLM_WATCHDOG_URL", "http://127.0.0.1:4000/health/liveliness")
CONTAINER = "litellm"
DB = "litellm_db"

HTTP_TIMEOUT = 5
FAIL_THRESHOLD = 2          # столько подряд неудачных проб => DOWN
ERR_WINDOW_MIN = 15         # окно анализа отказов
ERR_MIN_CALLS = 10          # меньше — не судим
ERR_RATIO = 0.5             # доля отказов, при которой тревога
ERR_ALERT_COOLDOWN = 1800   # не чаще раза в 30 минут
AGENT_COOLDOWN = 1800       # не поднимать разбирающего агента чаще раза в 30 минут


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


def psql(sql: str, timeout: int = 30) -> tuple[bool, str]:
    """Запрос к базе LiteLLM. Только чтение, без секретов в выводе."""
    try:
        out = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", DB, "-tAc", sql],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            log(f"psql failed rc={out.returncode}: {out.stderr.strip()[:200]}")
            return False, out.stderr.strip()[:400]
        return True, out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        log(f"psql exception: {exc}")
        return False, str(exc)


def probe_error_rate() -> tuple[bool, str, str]:
    """Доля отказов за окно по РЕАЛЬНЫМ вызовам моделей.

    Админские/UI-endpoint'ы прокси (`/v2/model/info`, `/get/user_banner`,
    `/model_group/info`, `/sso/get/ui_settings`, `/team/list`,
    `/get/latest_release_info` …) LiteLLM тоже пишет в спенд-логи и помечает
    `status<>'success'`, хотя модель не вызывалась. Они идут пачками от поллинга
    веб-интерфейса: на пустом окне это давало 14 из 15 «отказов» (93%) при нуле
    реальных ошибок. Поэтому всё, чей call_type начинается с `/`, отбрасываем.
    """
    where = (
        f"\"startTime\" > now() - interval '{ERR_WINDOW_MIN} minutes' "
        "and call_type not like '/%' and call_type <> 'pass_through_endpoint'"
    )
    ok, out = psql(f"select count(*), count(*) filter (where status <> 'success') "
                   f"from \"LiteLLM_SpendLogs\" where {where};")
    if not ok:
        return False, "", ""
    try:
        total, bad = (int(x) for x in out.split("|"))
    except ValueError:
        return False, "", ""
    if total < ERR_MIN_CALLS:
        return False, "", ""
    ratio = bad / total
    if ratio < ERR_RATIO:
        return False, "", ""

    detail = f"{bad} из {total} вызовов с ошибкой за {ERR_WINDOW_MIN} мин ({ratio:.0%})"
    _, rows = psql(
        "select coalesce(nullif(model_group,''),'(без группы)') || ' / ' || call_type || ' — ' || count(*) "
        f"from \"LiteLLM_SpendLogs\" where {where} and status <> 'success' "
        "group by 1 order by count(*) desc limit 5;"
    )
    breakdown = "; ".join(r for r in rows.splitlines() if r.strip())
    return True, detail, breakdown


def collect(reason: str, detail: str, container: str, drill: bool = False) -> Path:
    """Складывает улики в /var/lib/litellm-incidents/<ts>-<reason>/ для агента."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = INCIDENTS / f"{stamp}-{reason}"
    d.mkdir(parents=True, exist_ok=True)

    alert = {
        "target": "litellm", "reason": reason, "detail": detail, "drill": drill,
        "detected_at_utc": stamp, "container": container,
        "liveness_url": LIVENESS_URL, "error_window_min": ERR_WINDOW_MIN,
    }
    (d / "alert.json").write_text(json.dumps(alert, ensure_ascii=False, indent=2))

    cmds = {
        "health.txt": "for u in /health/liveliness /health/readiness /health; do "
                      "printf '%s -> ' \"$u\"; curl -s -o /dev/null -m 5 -w '%{http_code}\\n' "
                      "\"http://127.0.0.1:4000$u\"; done",
        "containers.txt": "docker ps -a --filter name=litellm --filter name=postgres "
                          "--format '{{.Names}} {{.Status}} {{.RunningFor}}'; echo; "
                          "docker inspect litellm --format 'restarts={{.RestartCount}} "
                          "exit={{.State.ExitCode}} oom={{.State.OOMKilled}} "
                          "started={{.State.StartedAt}} policy={{.HostConfig.RestartPolicy.Name}}'",
        "resource.txt": "uptime; free -m | head -2; df -h / /var/lib/docker | tail -2; "
                        "systemctl --failed --no-legend | head -10",
        "logs.txt": "docker logs --tail 200 --timestamps litellm 2>&1 | tail -200",
    }
    for name, cmd in cmds.items():
        try:
            out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
            (d / name).write_text((out.stdout or "") + (("\n[stderr]\n" + out.stderr) if out.stderr else ""))
        except Exception as exc:  # noqa: BLE001
            (d / name).write_text(f"collect failed: {exc}")

    # Срез по вызовам за окно: кто именно падает (группа/провайдер/код ответа).
    trend_sql = (
        "select to_char(\"startTime\",'HH24:MI') || ' ' || coalesce(nullif(model_group,''),'-') "
        "|| ' / ' || call_type || ' / ' || status from \"LiteLLM_SpendLogs\" "
        f"where \"startTime\" > now() - interval '{max(ERR_WINDOW_MIN, 60)} minutes' "
        "order by \"startTime\" desc limit 150;"
    )
    _, trend = psql(trend_sql)
    (d / "spendlog.txt").write_text(trend or "no rows")

    fails_sql = (
        "select to_char(\"startTime\",'HH24:MI') || ' | ' || coalesce(nullif(model_group,''),'-') "
        "|| ' | ' || coalesce(nullif(custom_llm_provider,''),'-') || ' | ' || call_type "
        "|| ' | ' || coalesce(left(response::text, 200),'') from \"LiteLLM_SpendLogs\" "
        "where status <> 'success' and \"startTime\" > now() - interval '24 hours' "
        "order by \"startTime\" desc limit 40;"
    )
    _, fails = psql(fails_sql)
    (d / "failures.txt").write_text(fails or "no failed calls in 24h")

    log(f"EVIDENCE {d}")
    return d


def agent_busy() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--plain", "--no-legend", "--all", "litellm-incident-*"],
            capture_output=True, text=True, timeout=15,
        )
        for line in out.stdout.splitlines():
            f = line.split()
            if len(f) >= 3 and f[2] == "running":
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def launch_agent(d: Path, state: dict, no_send: bool) -> None:
    """systemd-run + hermes chat: агент разбирается сам и отчитывается в Telegram."""
    now = int(time.time())
    if now - int(state.get("last_agent", 0)) < AGENT_COOLDOWN or agent_busy():
        log("agent launch skipped (cooldown or already running)")
        return
    unit = f"litellm-incident-{now}"
    cmd = ["systemd-run", "--collect", f"--unit={unit}",
           "--property=Nice=5", "--property=TimeoutStartSec=1800",
           "/bin/bash", AGENT, str(d)]
    if no_send:
        print("DRY: " + " ".join(cmd))
        return
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        log(f"AGENT LAUNCH {unit} rc={out.returncode} {out.stdout.strip()[:160]} {out.stderr.strip()[:160]}")
        state["last_agent"] = now
    except Exception as exc:  # noqa: BLE001
        log(f"AGENT LAUNCH failed: {exc}")


def main() -> int:
    load_env()
    no_send = "--no-send" in sys.argv
    drill = "--drill" in sys.argv

    if "--test" in sys.argv:
        send("🛡 <b>LiteLLM watchdog</b>: тест доставки. Шлюз под наблюдением.", no_send)
        return 0

    state = read_state()
    fails = int(state.get("fails", 0))
    down = bool(state.get("down", False))
    now = int(time.time())

    if drill:
        # Учебная тревога: гоняем ровно тот же путь, что и боевая, но текст помечен DRILL,
        # а состояние даунтайма не трогаем — иначе следующий тик пришлёт ложное «снова отвечает».
        container = probe_container()
        ok, detail = probe_http()
        text = (
            "🧪 <b>DRILL: LiteLLM watchdog</b>\n"
            f"Проба: {detail} (ok={ok}), контейнер: <code>{container}</code>\n"
            "Учебная тревога: проверяю цепочку «уведомление → улики → пробуждение агента»."
        )
        send(text, no_send)
        d = collect("drill", f"учебная тревога, проба {detail}", container, drill=True)
        launch_agent(d, state, no_send)
        write_state(state)
        return 0

    if PAUSE_FILE.exists():
        log("paused by pause file — no probes, no alarms")
        state.update({"last_probe": now, "last_detail": "paused"})
        write_state(state)
        return 0

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
                "Поднимаю агента на разбор — отчёт придёт отдельным сообщением."
            )
            send(text, no_send)
            log(f"down alert: {detail} container={container}")
            down = True
            d = collect("down", f"{fails} провалов пробы подряд ({detail})", container)
            launch_agent(d, state, no_send)

    alarm, err_detail, breakdown = probe_error_rate()
    if alarm:
        last = int(state.get("last_err_alert", 0))
        if now - last >= ERR_ALERT_COOLDOWN:
            text = (
                "⚠️ <b>LiteLLM: всплеск отказов</b>\n"
                f"{err_detail}\n"
                + (f"<code>{breakdown}</code>\n" if breakdown else "")
                + f"Контейнер: <code>{container}</code>\n"
                "Шлюз жив, но вызовы моделей падают. Поднимаю агента на разбор."
            )
            send(text, no_send)
            log(f"error-rate alert: {err_detail} [{breakdown}]")
            state["last_err_alert"] = now
            d = collect("errors", f"{err_detail} [{breakdown}]", container)
            launch_agent(d, state, no_send)

    state.update({"fails": fails, "down": down, "last_probe": now, "last_detail": detail})
    write_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
