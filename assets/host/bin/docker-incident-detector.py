#!/usr/bin/env python3
"""Детектор инцидентов с контейнерами: будит Hermes-агента на разбор.

Сам ничего не «чинит». Задача — заметить аномалию, собрать доказательства и
поднять полноценного агента (/usr/local/bin/docker-incident-agent.sh), который
разберётся в ПРИЧИНЕ, починит безопасными действиями или напишет владельцу.

Детектируется:
  * exited / dead при restart: always|unless-stopped дольше EXITED_GRACE_S;
  * crash-loop (Restarting) с растущим RestartCount;
  * health=unhealthy HEALTHY_HITS проверок подряд (stateful — больше);
  * скачок RestartCount между проверками (флаппинг, который можно не застать);
  * контейнер из allowlist исчез.

Анти-спам: 2 подтверждения подряд для exited/unhealthy, кулдаун COOLDOWN_S на
контейнер, одновременно работает не больше одного агента.

Флаги:
  --dry-run  только напечатать решения, агента не поднимать
  --status   состояние и выход

Таймер: docker-autorecover.timer (60 c).  Лог: /var/log/docker-autorecover.log
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = Path("/home/ai_service/notifications/.env")
CONF_FILE = Path(os.environ.get("AUTORECOVER_CONFIG", "/etc/docker-autorecover.conf"))
STATE_FILE = Path("/var/lib/docker-autorecover/state.json")
PAUSE_FILE = Path("/var/lib/docker-autorecover/pause")
INCIDENTS = Path("/var/lib/docker-incidents")
LOG_FILE = Path("/var/log/docker-autorecover.log")
AGENT = "/usr/local/bin/docker-incident-agent.sh"

EXITED_GRACE_S = int(os.environ.get("AUTORECOVER_EXITED_GRACE", "90"))
HEALTHY_HITS = int(os.environ.get("AUTORECOVER_UNHEALTHY_HITS", "2"))
HEALTHY_HITS_STATEFUL = int(os.environ.get("AUTORECOVER_UNHEALTHY_HITS_STATEFUL", "3"))
COOLDOWN_S = int(os.environ.get("AUTORECOVER_COOLDOWN", "1200"))
DISK_CRIT_PCT = 97
DRY = "--dry-run" in sys.argv

FMT = ("{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}|"
       "{{.RestartCount}}|{{.State.FinishedAt}}|{{.HostConfig.RestartPolicy.Name}}|"
       "{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}|{{.Config.Image}}")


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
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    if DRY:
        sys.stdout.write(line)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log(f"state write failed: {exc}")


def sh(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def send(text: str) -> None:
    if DRY:
        log(f"ALERT(dry): {text}")
        return
    token = os.environ.get("BOT_TOKEN", "")
    chat = os.environ.get("BOT_OWNER", "")
    if not token or not chat:
        log("BOT_TOKEN/BOT_OWNER missing — alert not sent")
        return
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        log(f"alert send failed: {exc}")


def load_allowlist() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not CONF_FILE.exists():
        log(f"config missing: {CONF_FILE}")
        return out
    for raw in CONF_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, flags = line.partition(":")
        out[name.strip()] = {"stateful": "stateful" in flags}
    return out


def inspect(name: str) -> dict | None:
    rc, out = sh(["docker", "inspect", "--format", FMT, name])
    if rc != 0:
        return None
    parts = out.split("|")
    if len(parts) < 9:
        return None
    return {
        "status": parts[0], "health": parts[1], "restarts": int(parts[2] or 0),
        "finished": parts[3], "policy": parts[4], "exit_code": parts[5],
        "oom": parts[6], "error": parts[7], "image": parts[8],
    }


def exited_seconds(finished: str) -> float:
    try:
        ts = finished.replace("Z", "+00:00")
        if "." in ts:
            head, tail = ts.split(".", 1)
            ts = f"{head}.{tail[:6]}{tail[6:] or '+00:00'}"
        when = datetime.fromisoformat(ts)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()
    except ValueError:
        return 0.0


def agent_busy() -> bool:
    rc, out = sh(["systemctl", "list-units", "--plain", "--no-legend",
                  "--all", "docker-incident-*"])
    if rc != 0:
        return False
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == "running":
            return True
    return False


def collect(name: str, info: dict, reason: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = INCIDENTS / f"{stamp}-{name}-{reason}"
    d.mkdir(parents=True, exist_ok=True)

    incident = {
        "container": name, "reason": reason, "detected_at_utc": stamp,
        "facts": info, "exited_for_s": round(exited_seconds(info["finished"]), 1),
    }
    (d / "incident.json").write_text(json.dumps(incident, ensure_ascii=False, indent=2))

    rc, out = sh(["docker", "inspect", name], timeout=30)
    (d / "inspect.json").write_text(out[:200_000])

    rc, out = sh(["docker", "logs", "--tail", "300", "--timestamps", name], timeout=60)
    (d / "logs.txt").write_text(out[:300_000])

    rc, out = sh(["bash", "-c",
                  "dmesg -T 2>/dev/null | grep -iE 'oom-kill|killed process|out of memory' | tail -20"], 30)
    (d / "oom.txt").write_text(out or "no oom events in ring buffer")

    rc, out = sh(["bash", "-c",
                  "df -h / /var/lib/docker; echo; docker system df"], 60)
    (d / "disk.txt").write_text(out)

    rc, out = sh(["bash", "-c",
                  "systemctl is-active postgresql nginx docker; echo; "
                  "uptime; free -m | head -2; echo; "
                  "systemctl --failed --no-legend | head -10"], 30)
    (d / "host.txt").write_text(out)

    rc, out = sh(["bash", "-c",
                  f"journalctl -u docker --since '30 min ago' --no-pager | grep -i {name} | tail -30"], 60)
    (d / "docker-journal.txt").write_text(out or "no docker journal lines for this container")

    log(f"INCIDENT {name} reason={reason} evidence={d}")
    return d


def launch(d: Path, name: str, reason: str) -> None:
    unit = f"docker-incident-{name}-{int(time.time())}"
    cmd = ["systemd-run", "--collect", f"--unit={unit}",
           "--property=Nice=5", "--property=TimeoutStartSec=1800",
           "/bin/bash", AGENT, str(d)]
    if DRY:
        log(f"DRY: would run {' '.join(cmd)}")
        return
    rc, out = sh(cmd, timeout=60)
    log(f"AGENT LAUNCH {unit} rc={rc} {out[:200]}")


def main() -> int:
    load_env()
    state = read_state()
    allow = load_allowlist()

    if "--status" in sys.argv:
        for name in allow:
            print(f"{name:26} {inspect(name)}")
        print("pause:", PAUSE_FILE.exists(), "| agent_busy:", agent_busy())
        return 0

    # Heartbeat: писать state.json на каждом тике, в том числе когда детектор
    # ничего не делает. Иначе ветки ниже (pause / docker not active) выходят,
    # не обновив файл, и docker-detector-liveness.sh после снятия паузы видит
    # возраст > 600 с и шлёт ложное «детектор мёртв».
    write_state(state)

    if PAUSE_FILE.exists():
        log("paused by flag file — nothing done")
        return 0

    if sh(["systemctl", "is-active", "docker"])[0] != 0:
        log("docker not active — skip")
        return 0

    usage = shutil.disk_usage("/")
    used_pct = usage.used / usage.total * 100
    if used_pct >= DISK_CRIT_PCT:
        g = state.setdefault("_global", {})
        if time.time() - g.get("alert_disk", 0) > 1800:
            g["alert_disk"] = time.time()
            send(f"💾 Диск занят на {used_pct:.0f}% — разбор инцидентов приостановлен, "
                 f"сначала место.")
            write_state(state)
        return 0

    present = set(sh(["docker", "ps", "-a", "--format", "{{.Names}}"])[1].split())
    now = time.time()

    for name, cfg in allow.items():
        rec = state.setdefault(name, {})
        rec["seen"] = now

        if name not in present:
            rec["missing"] = rec.get("missing", 0) + 1
            if rec["missing"] >= 2 and now - rec.get("last_incident", 0) > COOLDOWN_S:
                rec["last_incident"] = now
                info = {"status": "missing"}
                d = collect(name, info, "missing")
                launch(d, name, "missing")
            write_state(state)
            continue
        rec["missing"] = 0

        info = inspect(name)
        if info is None:
            continue

        prev_restarts = rec.get("restarts", info["restarts"])
        restarts_delta = info["restarts"] - prev_restarts
        rec["restarts"] = info["restarts"]

        reason = ""
        if info["status"] in ("exited", "dead") and info["policy"] in ("always", "unless-stopped"):
            if exited_seconds(info["finished"]) >= EXITED_GRACE_S:
                rec["exited_hits"] = rec.get("exited_hits", 0) + 1
                if rec["exited_hits"] >= 2:
                    reason = "exited"
        else:
            rec["exited_hits"] = 0

        if info["status"] == "restarting" and restarts_delta > 0:
            reason = "crash_loop"
        elif restarts_delta >= 2 and info["status"] == "running":
            reason = "restart_flap"

        if info["health"] == "unhealthy":
            rec["unhealthy_hits"] = rec.get("unhealthy_hits", 0) + 1
            threshold = HEALTHY_HITS_STATEFUL if cfg["stateful"] else HEALTHY_HITS
            if rec["unhealthy_hits"] >= threshold:
                reason = reason or "unhealthy"
        else:
            rec["unhealthy_hits"] = 0

        if not reason:
            continue

        if now - rec.get("last_incident", 0) <= COOLDOWN_S:
            log(f"SKIP {name} reason={reason}: cooldown "
                f"{int(now - rec.get('last_incident', 0))}s/{COOLDOWN_S}s")
            continue

        if agent_busy():
            log(f"SKIP {name} reason={reason}: agent already running")
            continue

        rec["last_incident"] = now
        rec["exited_hits"] = 0
        rec["unhealthy_hits"] = 0
        write_state(state)
        d = collect(name, info, reason)
        launch(d, name, reason)

    write_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
