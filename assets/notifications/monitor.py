#!/usr/bin/env python3
"""Small server monitor that sends Telegram notifications."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tenacity import Retrying, retry_if_exception_type, wait_exponential

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding exported environment vars."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")


class MonitorError(RuntimeError):
    """Raised for expected monitoring failures."""


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def send_telegram(
    text: str,
    parse_mode: str | None = "HTML",
    token: str | None = None,
    chat_id: str | None = None,
) -> None:
    token = token or os.environ.get("BOT_TOKEN")
    chat_id = chat_id or os.environ.get("BOT_OWNER")
    if not token or not chat_id:
        raise MonitorError("В .env должны быть заданы BOT_TOKEN и BOT_OWNER (Telegram chat ID).")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    payload = urlencode(data).encode("utf-8")
    request = Request(url, data=payload, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Telegram's HTTPError body contains useful details (for example, "chat not found").
        try:
            body = json.loads(exc.read().decode("utf-8"))
            error = str(body.get("description") or exc)
        except (UnicodeDecodeError, json.JSONDecodeError):
            error = str(exc)
        safe_error = error.replace(token, "<redacted>")
        raise MonitorError(f"Telegram API HTTP {exc.code}: {safe_error}") from exc
    except Exception as exc:  # noqa: BLE001 - network and API failures should be reported together
        # urllib errors may include the request URL, which contains the bot token.
        safe_error = str(exc).replace(token, "<redacted>")
        raise MonitorError(f"Не удалось отправить сообщение в Telegram: {safe_error}") from exc
    if not result.get("ok"):
        raise MonitorError(f"Telegram Bot API вернул ошибку: {result}")


def send_long_message(
    text: str, limit: int = 3900, parse_mode: str = "HTML", chat_id: str | None = None
) -> None:
    """Split long notifications below Telegram's 4096-char limit.

    With `parse_mode="HTML"` chunks must not cut a tag in half: Telegram rejects a chunk
    whose `<pre>`/`<b>` is left open ("can't parse entities: Can't find end tag"), which is
    exactly how crash/OOM alerts with long docker logs were lost. Each chunk closes the tags
    it leaves open and the next chunk reopens them.
    """
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""
    for line in lines:
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            cut = line.rfind(">", 0, limit) + 1 if ">" in line[:limit] else limit
            chunks.append(line[:cut])
            line = line[cut:]
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)

    if parse_mode and parse_mode.upper() == "HTML":
        chunks = _balance_html_chunks(chunks)
    for chunk in chunks:
        send_telegram(chunk, parse_mode=parse_mode, chat_id=chat_id)
        time.sleep(0.1)  # Small delay to avoid Telegram rate limits


HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)>")
SELF_CLOSING_TAGS = {"br", "hr"}


def _open_tags(text: str) -> list[str]:
    """Tags left open at the end of `text`, in opening order."""
    stack: list[str] = []
    for match in HTML_TAG_RE.finditer(text):
        closing, name, selfclose = match.group(1), match.group(2).lower(), match.group(3)
        if name in SELF_CLOSING_TAGS or selfclose:
            continue
        if closing:
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return stack


def _balance_html_chunks(chunks: list[str]) -> list[str]:
    """Close dangling tags at the end of each chunk and reopen them in the next one."""
    balanced: list[str] = []
    pending: list[str] = []
    for chunk in chunks:
        body = "".join(f"<{tag}>" for tag in pending) + chunk
        stack = _open_tags(body)
        balanced.append(body + "".join(f"</{tag}>" for tag in reversed(stack)))
        pending = stack
    return balanced


def state_file() -> Path:
    return env_path("MONITOR_STATE_FILE", str(BASE_DIR / ".monitor_state.json"))


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(state, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def docker_binary() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise MonitorError("Не найден docker CLI. Установите Docker CLI и проверьте права пользователя.")
    return docker


def docker_logs_tail(docker: str, container_id: str, tail: int) -> str:
    """Read the last <tail> log lines of a container (on demand, no live subscription)."""
    try:
        result = subprocess.run(
            [docker, "logs", "--tail", str(tail), container_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"(не удалось получить логи: {exc})"
    logs = ((result.stdout or "") + (result.stderr or "")).strip()
    return logs if logs else "(логи пусты)"


def docker_inspect_state(docker: str, container_id: str) -> dict[str, Any]:
    """Exit code, status, OOM flag, error and memory limit of a container."""
    info: dict[str, Any] = {
        "name": container_id[:12],
        "status": "unknown",
        "exit_code": None,
        "oom_killed": False,
        "error": "",
        "memory_limit": 0,
    }
    try:
        result = subprocess.run(
            [
                docker,
                "inspect",
                "--format",
                "{{.Name}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|"
                "{{.State.Error}}|{{.HostConfig.Memory}}",
                container_id,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return info
    parts = result.stdout.strip().split("|")
    if len(parts) < 6:
        return info
    info["name"] = parts[0].lstrip("/") or info["name"]
    info["status"] = parts[1].strip() or "unknown"
    try:
        info["exit_code"] = int(parts[2])
    except (TypeError, ValueError):
        pass
    info["oom_killed"] = parts[3].strip().lower() == "true"
    info["error"] = parts[4].strip()
    try:
        info["memory_limit"] = int(parts[5])
    except (TypeError, ValueError):
        pass
    return info


def docker_mem_usage(docker: str, container_id: str) -> str | None:
    """One-shot memory snapshot (`docker stats --no-stream`); None if unavailable."""
    try:
        result = subprocess.run(
            [docker, "stats", "--no-stream", "--format", "{{.MemUsage}} / {{.MemPerc}}", container_id],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def oom_kernel_markers(container_name: str) -> str:
    """Grep dmesg/syslog for OOM markers related to the container (best-effort)."""
    needles = ("oom", "killed process", "out of memory")
    hits: list[str] = []
    commands = (
        ["dmesg", "-T"],
        ["grep", "-iE", "oom|killed process|out of memory", "/var/log/syslog"],
    )
    for command in commands:
        try:
            output = subprocess.run(
                command, capture_output=True, text=True, timeout=5
            ).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in output.splitlines()[-400:]:
            low = line.lower()
            if container_name.lower() in low or any(needle in low for needle in needles):
                hits.append(line.strip())
    if not hits:
        return "маркеры OOM в dmesg/syslog не найдены"
    return "\n".join(list(dict.fromkeys(hits))[-5:])


def escape_logs(text: str, limit: int = 3500) -> str:
    """HTML-escape log text and cap its size before embedding into <pre>."""
    cleaned = (text or "").replace("\x00", "")
    if len(cleaned) > limit:
        cleaned = "…" + cleaned[-limit:]
    return html.escape(cleaned)


def _log_block(title: str, logs: str) -> str:
    return f"<b>{title}</b>\n<pre>{escape_logs(logs)}</pre>"


# Events we subscribe to on the Docker Events API.
EVENT_ACTIONS = ("create", "start", "die", "stop", "restart", "oom")

# container state -> (label for /docker_ps)
_STATE_LABELS = {
    "running": "Up",
    "exited": "Exited",
    "restarting": "Restarting",
    "created": "Created",
    "paused": "Paused",
    "removing": "Removing",
    "dead": "Dead",
}


class DockerEventStreamError(RuntimeError):
    """The `docker events` stream ended or could not be started."""


class DockerEventWatcher:
    """Long-lived Docker Events subscriber with batching, throttling and retries.

    Batching and throttling share one window (MONITOR_EVENT_WINDOW_SECONDS, default
    60 s): events arriving inside a window are aggregated into a single message, and
    because a window takes at least that long, notifications are never sent more
    often than once per window.
    """

    def __init__(self, docker: str | None = None) -> None:
        self.docker = docker or docker_binary()
        self.window = max(float(os.environ.get("MONITOR_EVENT_WINDOW_SECONDS", "60")), 1.0)
        self.restart_cooldown = max(
            float(os.environ.get("MONITOR_RESTART_COOLDOWN_SECONDS", "600")), 1.0
        )
        self.start_tail = int(os.environ.get("MONITOR_EVENT_LOG_TAIL_START", "50"))
        self.stop_tail = int(os.environ.get("MONITOR_EVENT_LOG_TAIL_STOP", "20"))
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending: dict[str, dict[str, Any]] = {}
        self.window_started: float | None = None
        self.last_send = 0.0
        self.restart_notified: dict[str, float] = {}
        self.oom_notified: dict[str, float] = {}
        self.down = False

    # ------------------------------------------------------------------ stream
    def _stream_once(self) -> None:
        """Consume `docker events` until the stream breaks, then raise to reconnect."""
        command = [self.docker, "events", "--format", "{{json .}}", "--filter", "type=container"]
        for action in EVENT_ACTIONS:
            command += ["--filter", f"event={action}"]
        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
        except OSError as exc:
            raise DockerEventStreamError(f"не удалось запустить docker events: {exc}") from exc
        self.inbox.put({"_monitor_reconnected": True})
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    self.inbox.put(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        detail = ""
        if proc.stderr is not None:
            try:
                detail = proc.stderr.read().strip()
            except (ValueError, OSError):
                detail = ""
        raise DockerEventStreamError(
            f"поток docker events оборвался (rc={proc.returncode})" + (f": {detail}" if detail else "")
        )

    def _before_sleep(self, retry_state: Any) -> None:
        attempt = getattr(retry_state, "attempt_number", 0)
        outcome = getattr(retry_state, "outcome", None)
        exc = outcome.exception() if outcome is not None else None
        print(f"events: переподключение #{attempt} ({exc})", file=sys.stderr, flush=True)
        self.down = True
        if attempt == 3:
            try:
                send_telegram(
                    f"⚠️ Docker events недоступен, повторяю попытки подключения: {exc}",
                    parse_mode=None,
                )
            except MonitorError:
                pass

    def _reader_loop(self) -> None:
        retrying = Retrying(
            retry=retry_if_exception_type(DockerEventStreamError),
            wait=wait_exponential(multiplier=1, min=1, max=60),
            before_sleep=self._before_sleep,
            reraise=True,
        )
        while True:
            try:
                retrying(self._stream_once)
                return
            except DockerEventStreamError as exc:  # pragma: no cover - retry loop is infinite
                print(f"events: поток остановлен: {exc}", file=sys.stderr, flush=True)
                return
            except Exception as exc:  # noqa: BLE001 - never let the reader die silently
                print(f"events: неожиданная ошибка потока: {exc}", file=sys.stderr, flush=True)
                return

    # -------------------------------------------------------------------- run
    def start(self) -> None:
        reader = threading.Thread(target=self._reader_loop, name="docker-events", daemon=True)
        reader.start()
        try:
            self._loop()
        except KeyboardInterrupt:  # pragma: no cover - systemd sends SIGTERM/SIGINT
            pass

    def _loop(self) -> None:
        print("events: наблюдение за Docker events запущено", flush=True)
        while True:
            try:
                event = self.inbox.get(timeout=1.0)
            except queue.Empty:
                event = None
            if isinstance(event, dict) and event.get("_monitor_reconnected"):
                self._notify_recovered()
            elif event is not None:
                self._accept(event)
            if (
                self.pending
                and self.window_started is not None
                and time.monotonic() - self.window_started >= self.window
            ):
                self._flush()

    def _notify_recovered(self) -> None:
        if not self.down:
            return
        self.down = False
        try:
            send_telegram("✅ Соединение с Docker events восстановлено.", parse_mode=None)
        except MonitorError:
            pass

    # --------------------------------------------------------------- batching
    def _accept(self, event: dict[str, Any]) -> None:
        action = str(event.get("Action") or event.get("status") or "").split(":")[0].strip().lower()
        if action not in EVENT_ACTIONS:
            return
        actor = event.get("Actor") or {}
        cid = str(actor.get("ID") or event.get("id") or "").strip()
        if not cid:
            return
        attributes = actor.get("Attributes") or {}
        name = str(attributes.get("name") or cid[:12])
        raw_exit = attributes.get("exitCode")
        try:
            exit_code = int(raw_exit) if raw_exit is not None else None
        except (TypeError, ValueError):
            exit_code = None

        entry = self.pending.setdefault(cid, {"name": name, "actions": [], "exit_codes": []})
        entry["name"] = name
        entry["actions"].append(action)
        if exit_code is not None:
            entry["exit_codes"].append(exit_code)
        if self.window_started is None:
            self.window_started = time.monotonic()

    def _flush(self) -> None:
        pending, self.pending = self.pending, {}
        self.window_started = None
        now = time.monotonic()
        blocks: list[str] = []
        for cid, entry in pending.items():
            try:
                block = self._render_block(cid, entry, now)
            except Exception as exc:  # noqa: BLE001 - one bad event must not kill the watcher
                print(f"events: ошибка обработки события {cid[:12]}: {exc}", file=sys.stderr, flush=True)
                block = f"⚠️ Не удалось обработать событие контейнера <code>{cid[:12]}</code>."
            if block:
                blocks.append(block)
        if not blocks:
            return
        if len(blocks) == 1:
            message = blocks[0]
        else:
            message = "🐳 <b>СОБЫТИЯ DOCKER</b>\n" + ("─" * 40) + "\n\n" + ("\n\n" + "─" * 40 + "\n\n").join(blocks)
        self._throttle()
        try:
            send_long_message(message, parse_mode="HTML")
        except MonitorError as exc:
            print(f"events: не удалось отправить уведомление: {exc}", file=sys.stderr, flush=True)
        self.last_send = time.monotonic()

    def _throttle(self) -> None:
        wait = self.window - (time.monotonic() - self.last_send)
        if wait > 0:
            time.sleep(wait)

    # ---------------------------------------------------------------- reports
    def _render_block(self, cid: str, entry: dict[str, Any], now: float) -> str:
        name = html.escape(str(entry["name"]))
        short = cid[:12]
        actions = entry["actions"]
        codes = entry["exit_codes"]
        if "oom" in actions:
            return self._oom_report(cid, name, short, codes, now)
        if any(action in actions for action in ("die", "stop")):
            return self._stop_report(cid, name, short, codes, now)
        if "restart" in actions:
            return self._restart_report(cid, name, short, codes, now)
        if "start" in actions or "create" in actions:
            return self._start_report(cid, name, short, created="start" not in actions)
        return ""

    def _stop_report(self, cid: str, name: str, short: str, codes: list[int], now: float) -> str:
        info = docker_inspect_state(self.docker, cid)
        exit_code = codes[-1] if codes else info["exit_code"]
        if info["oom_killed"]:
            return self._oom_report(cid, name, short, codes, now)
        if info["status"] == "restarting":
            return self._restart_report(cid, name, short, codes, now)
        if exit_code == 0 and not info["error"]:
            return (
                f"ℹ️ Контейнер <b>{name}</b> остановлен штатно. "
                f"(<code>{short}</code>, exit 0)"
            )
        reason = f"exit {exit_code}" if exit_code is not None else (info["error"] or "неизвестно")
        if info["error"]:
            reason = f"{reason} | {info['error']}"
        logs = docker_logs_tail(self.docker, cid, self.stop_tail)
        return (
            "🚨 <b>КОНТЕЙНЕР УПАЛ</b>\n"
            f"{'─' * 40}\n"
            f"<b>Контейнер:</b> {name} (<code>{short}</code>)\n"
            f"<b>Причина:</b> {html.escape(reason)}\n\n"
            f"{_log_block(f'Последние {self.stop_tail} строк логов:', logs)}"
        )

    def _restart_report(self, cid: str, name: str, short: str, codes: list[int], now: float) -> str:
        last = self.restart_notified.get(cid)
        if last is not None and now - last < self.restart_cooldown:
            return ""  # already reported this restart loop; wait for the status to change
        self.restart_notified[cid] = now
        exit_code = codes[-1] if codes else None
        reason = f"exit {exit_code}" if exit_code is not None else "restarting"
        logs = docker_logs_tail(self.docker, cid, self.stop_tail)
        return (
            "🔁 <b>ЦИКЛИЧЕСКИЙ РЕСТАРТ</b>\n"
            f"{'─' * 40}\n"
            f"<b>Контейнер:</b> {name} (<code>{short}</code>)\n"
            f"<b>Статус:</b> restarting ({html.escape(reason)})\n\n"
            f"{_log_block(f'Последние {self.stop_tail} строк логов:', logs)}"
        )

    def _oom_report(self, cid: str, name: str, short: str, codes: list[int], now: float) -> str:
        # An OOM kill emits `oom` and then `die`; report the pair once per cooldown.
        last = self.oom_notified.get(cid)
        if last is not None and now - last < self.restart_cooldown:
            return ""
        self.oom_notified[cid] = now
        self.restart_notified.pop(cid, None)
        info = docker_inspect_state(self.docker, cid)
        exit_code = codes[-1] if codes else info["exit_code"]
        limit = info["memory_limit"]
        limit_text = format_mb_gb(limit) if limit else "не задан"
        total, used, free = memory_snapshot()
        cause = "OOMKilled"
        if exit_code not in (None, 0):
            cause += f" (exit {exit_code})"
        lines = [
            "🚨 <b>[OOM] КОНТЕЙНЕР УБИТ ПО НЕХВАТКЕ ПАМЯТИ</b>",
            "─" * 40,
            f"<b>Контейнер:</b> {name} (<code>{short}</code>)",
            f"<b>Причина:</b> {cause}",
            f"<b>Лимит памяти контейнера:</b> {limit_text}",
        ]
        usage = docker_mem_usage(self.docker, cid)
        if usage and not usage.replace(" ", "").startswith("0B/"):
            lines.append(f"<b>Память контейнера (снимок):</b> {html.escape(usage)}")
        lines.append(
            f"<b>Память хоста:</b> занято {format_mb_gb(used)} | {percent_of(used, total):.1f}%, "
            f"свободно {format_mb_gb(free)} | {percent_of(free, total):.1f}%, всего {format_mb_gb(total)}"
        )
        markers = oom_kernel_markers(str(info["name"]))
        lines.append(f"<b>Маркеры ядра:</b>\n<pre>{escape_logs(markers)}</pre>")
        logs = docker_logs_tail(self.docker, cid, self.stop_tail)
        lines.append(_log_block(f"Последние {self.stop_tail} строк логов:", logs))
        return "\n".join(lines)

    def _start_report(self, cid: str, name: str, short: str, created: bool = False) -> str:
        self.restart_notified.pop(cid, None)
        self.oom_notified.pop(cid, None)
        logs = docker_logs_tail(self.docker, cid, self.start_tail)
        verb = "создан" if created else "запущен"
        icon = "🆕" if created else "✅"
        return (
            f"{icon} Контейнер <b>{name}</b> {verb}. (<code>{short}</code>)\n"
            f"{_log_block(f'Последние {self.start_tail} строк логов:', logs)}"
        )


def watch_docker_events() -> None:
    """Run the long-lived Docker events watcher (systemd: server-monitor-events.service)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    DockerEventWatcher().start()


def _split_container_status(state: str, status: str) -> tuple[str, str]:
    """Map a `docker ps` State/Status pair onto (<Up|Exited|Restarting>, uptime/downtime)."""
    label = _STATE_LABELS.get(state, state.title() or "Unknown")
    text = status.strip()
    timing = text
    if state == "running" and text.lower().startswith("up"):
        timing = text[2:].strip()
    elif state in ("exited", "restarting", "paused", "dead", "created"):
        match = re.search(r"\)\s*(.+)$", text)
        if match:
            timing = match.group(1).strip()
    return label, timing or text


def build_docker_ps_report() -> str:
    """Snapshot of all containers: `<name> — <status> | <uptime/downtime>`."""
    docker = docker_binary()
    try:
        result = subprocess.run(
            [docker, "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise MonitorError(f"Не удалось получить список контейнеров: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    lines = ["🐳 <b>DOCKER PS</b>", "─" * 40]
    if not rows:
        lines.append("Контейнеры не найдены.")
        return "\n".join(lines)
    rows.sort(key=lambda item: str(item.get("Names", "")).lower())
    for item in rows:
        name = str(item.get("Names") or "?")
        state = str(item.get("State") or "").lower()
        status = str(item.get("Status") or "")
        label, timing = _split_container_status(state, status)
        lines.append(f"<b>{html.escape(name)}</b> — {label} | {html.escape(timing)}")
    return "\n".join(lines)


def docker_ps(chat_id: str | None = None) -> None:
    send_long_message(build_docker_ps_report(), parse_mode="HTML", chat_id=chat_id)


def parse_dump_date(filename: str) -> str:
    """Parse common YYYY-MM-DD / YYYYMMDD dates (optionally with time) in a filename."""
    custom_pattern = os.environ.get("BACKUP_DATE_REGEX")
    if custom_pattern:
        try:
            match = re.search(custom_pattern, filename)
        except re.error as exc:
            raise MonitorError(f"Некорректный BACKUP_DATE_REGEX: {exc}") from exc
        if not match:
            return "дата в имени не распознана"
        raw = match.groupdict().get("date") or match.group(0)
        return raw

    match = re.search(
        r"(?<!\d)(\d{4})[-_]?([01]\d)[-_]?([0-3]\d)"
        r"(?:[T _-]?([0-2]\d)[-:]?([0-5]\d)(?:[-:]?([0-5]\d))?)?(?!\d)",
        filename,
    )
    if not match:
        return "дата в имени не распознана"
    year, month, day, hour, minute, second = match.groups()
    try:
        parsed = datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0)
        )
    except ValueError:
        return "дата в имени не распознана"
    return parsed.strftime("%Y-%m-%d %H:%M:%S" if hour is not None else "%Y-%m-%d")


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} ТиБ"


# Telegram sendDocument takes 50 MB, but getFile — the call a restore uses to pull the dump
# back — stops at 20 MB. Parts therefore have to fit the DOWNLOAD limit, otherwise the backup
# sits in the chat and cannot be recovered on a new server. 18 MB leaves a small margin.
TG_DOC_LIMIT = 18 * 1024 * 1024
# file_id реестр: последние N наборов на базу (реестр уходит в git-снапшот, он должен быть мал).
TG_REGISTRY_SETS_PER_DB = 3
TG_REGISTRY_NAME = ".tg-registry.json"
# Databases reported first, in this order; any other DB is appended alphabetically.
REPORT_DB_ORDER = ("webui_db", "litellm_db")
# /backups restores the newest dump of each DB into a throwaway database to prove it is
# actually usable. Dumps Telegram receives split into parts are re-split and re-joined
# first, so the verified artifact is the same byte stream the recipient gets.
VERIFY_DB_PREFIX = "verify_restore_"
VERIFY_SPLIT_DBS = ("webui_db",)
VERIFY_TIMEOUT_SECONDS = 300
# pgvector index builds need more than the 64 MB default of maintenance_work_mem: restoring
# webui_db failed with "memory required is 65 MB". Set it for the restore session only.
VERIFY_MAINTENANCE_WORK_MEM = "512MB"


def parse_dump_timestamp(filename: str) -> datetime | None:
    """Extract the YYYYMMDD_HHMMSS timestamp embedded in a dump filename."""
    match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", filename)
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def db_name_from_file(path: Path) -> str:
    """webui_db_20261011_063001.dump -> webui_db"""
    match = re.match(r"^(?P<db>.+?)_\d{8}_\d{6}\.dump$", path.name)
    return match.group("db") if match else path.stem


def split_part_count(size: int, limit: int = TG_DOC_LIMIT) -> int:
    """How many byte-equal parts `size` needs so that no part exceeds `limit`.

    `split -n N` produces chunks of ceil(size/N) bytes, so N = ceil(size/limit) is the
    smallest count that keeps every part inside Telegram's limit. A fixed 2 parts silently
    produced oversized chunks once webui_db grew past 90 MB — the parts then failed to
    upload and the failure only surfaced as a warning line inside the digest.
    """
    if size <= limit:
        return 1
    return -(-size // limit)


def split_dump(path: Path, parts: int | None = None) -> list[Path]:
    """Split a dump into byte-equal chunks named <name>.part_00, .part_01, ...

    `parts` defaults to whatever keeps each chunk within TG_DOC_LIMIT, so the part count
    follows the dump size instead of being pinned at 2.
    """
    if parts is None:
        parts = split_part_count(path.stat().st_size)
    subprocess.run(
        ["split", "-n", str(parts), "-d", str(path), f"{path}.part_"],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(path.parent.glob(f"{path.name}.part_*"))


def sha256_of(path: Path) -> str:
    """SHA-256 файла — попадает в инструкцию по склейке для проверки целостности."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Russian plural agreement by count: 1 -> one, 2-4 -> few, 0/5+ -> many."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def build_split_instruction(
    latest: Path, part_sizes: list[tuple[str, int]], digest: str | None, failed: bool
) -> str:
    """Инструкция для файлового бота: как склеить и как проверить архив."""
    total = sum(size for _, size in part_sizes)
    names = " ".join(html.escape(name) for name, _ in part_sizes)
    lines = [
        f"🧩 <b>Архив отправлен {plural_ru(len(part_sizes), 'частью', 'частями', 'частями')}</b>",
        f"Файл: <code>{html.escape(latest.name)}</code> ({format_size(total)})",
        "",
        "Части:",
    ]
    for index, (name, size) in enumerate(part_sizes, start=1):
        lines.append(f"  {index}. <code>{html.escape(name)}</code> — {format_size(size)}")
    lines += [
        "",
        f"<b>Как собрать</b> ({plural_ru(len(part_sizes), 'часть', 'обе части', 'все части')} в одной папке):",
        f"<code>cat {names} &gt; {html.escape(latest.name)}</code>",
    ]
    if failed:
        lines += [
            "",
            "⚠️ Часть файлов не доставлена — повторите отправку: "
            "<code>python3 /home/ai_service/notifications/monitor.py backup</code>",
        ]
    elif digest:
        lines += [
            "",
            "<b>Проверка целостности</b> (должна совпасть с указанным хешем):",
            f"<code>sha256sum {html.escape(latest.name)}</code>",
            f"Ожидаемый SHA-256: <code>{digest}</code>",
        ]
    return "\n".join(lines)


def send_document(token: str, chat_id: str, path: Path, caption: str | None = None) -> dict:
    """Upload a file to Telegram via sendDocument (multipart/form-data).

    Returns the Telegram identifiers of the uploaded document: a bot cannot read its own chat
    history, so `file_id` has to be stored now to be able to download the file later.
    """
    boundary = "----HermesBoundary" + uuid.uuid4().hex
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )

    add_field("chat_id", str(chat_id))
    if caption:
        add_field("caption", caption)
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    chunks.append(path.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            error = str(error_body.get("description") or exc)
        except (UnicodeDecodeError, json.JSONDecodeError):
            error = str(exc)
        raise MonitorError(f"Telegram sendDocument HTTP {exc.code}: {error.replace(token, '<redacted>')}") from exc
    except Exception as exc:  # noqa: BLE001
        raise MonitorError(
            f"Не удалось отправить файл в Telegram: {str(exc).replace(token, '<redacted>')}"
        ) from exc
    if not result.get("ok"):
        raise MonitorError(f"Telegram Bot API вернул ошибку при отправке файла: {result}")
    document = (result.get("result") or {}).get("document") or {}
    reported_size = document.get("file_size")
    expected_size = path.stat().st_size
    if isinstance(reported_size, int) and reported_size != expected_size:
        raise MonitorError(
            f"Telegram принял {path.name} размером {reported_size} Б вместо {expected_size} Б — "
            "файл загружен не полностью."
        )
    return {
        "message_id": (result.get("result") or {}).get("message_id"),
        "file_id": document.get("file_id"),
        "file_unique_id": document.get("file_unique_id"),
        "file_size": reported_size if isinstance(reported_size, int) else expected_size,
    }


def tg_registry_path() -> Path:
    """Где лежит реестр file_id отправленных дампов.

    Бот не умеет читать свою историю чата, поэтому единственная зацепка для восстановления —
    file_id, который вернул sendDocument. Реестр пишется рядом с дампами и уезжает в приватный
    git-снапшот (`host/backups/tg-registry.json`), откуда его читает install.py.
    """
    explicit = os.environ.get("TG_REGISTRY")
    if explicit:
        return Path(explicit).expanduser()
    return env_path("BACKUP_DIR", "/var/backups") / TG_REGISTRY_NAME


def record_backup_registry(entry: dict) -> Path:
    """Добавить набор (дамп + части) в реестр, оставив N последних наборов на базу."""
    path = tg_registry_path()
    data: dict[str, Any] = {"sets": []}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {"sets": []}
    sets = [
        item
        for item in data.get("sets", [])
        if isinstance(item, dict) and item.get("dump") != entry.get("dump")
    ]
    sets.append(entry)
    kept: list[dict] = []
    counts: dict[str, int] = {}
    for item in sorted(sets, key=lambda s: str(s.get("created_at", "")), reverse=True):
        db = str(item.get("db", ""))
        if counts.get(db, 0) >= TG_REGISTRY_SETS_PER_DB:
            continue
        counts[db] = counts.get(db, 0) + 1
        kept.append(item)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chat_id": str(entry.get("chat_id", "")),
        "sets": kept,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def backup_registry_entry(
    db: str, dump: Path, chat_id: str, parts: list[tuple[Path, dict]], complete: bool
) -> dict:
    """Собрать запись реестра: дамп, его sha256 и по каждой части file_id + message_id."""
    return {
        "db": db,
        "dump": dump.name,
        "size": dump.stat().st_size,
        "sha256": sha256_of(dump),
        "created_at": datetime.fromtimestamp(
            dump.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chat_id": str(chat_id),
        "complete": complete,
        "parts": [
            {
                "name": part.name,
                "size": part.stat().st_size,
                "sha256": sha256_of(part),
                "file_id": sent.get("file_id"),
                "message_id": sent.get("message_id"),
            }
            for part, sent in parts
        ],
    }


def collect_backup_groups() -> tuple[dict[str, list[Path]], list[str], list[Path]]:
    """Group backup dumps by database (newest first). Returns (groups, order, files)."""
    backup_dir = env_path("BACKUP_DIR", "/var/backups").resolve()
    pattern = os.environ.get("BACKUP_PATTERN", "*.dump")
    files = sorted(
        (path for path in backup_dir.glob(pattern) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise MonitorError(f"Не найдены бэкапы в {backup_dir} по шаблону {pattern!r}.")

    groups: dict[str, list[Path]] = {}
    for path in files:
        groups.setdefault(db_name_from_file(path), []).append(path)
    for paths in groups.values():
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    order = [name for name in REPORT_DB_ORDER if name in groups]
    order += sorted(name for name in groups if name not in REPORT_DB_ORDER)
    return groups, order, files


def build_backup_digest(
    groups: dict[str, list[Path]], order: list[str], files: list[Path]
) -> str:
    """Backup inventory digest — the exact text the nightly report sends."""

    def fmt_dt(path: Path) -> str:
        ts = parse_dump_timestamp(path.name)
        return ts.strftime("%d.%m.%Y %H:%M") if ts else "?"

    lines: list[str] = []
    for name in order:
        lines.append(f"Создан бэкап {name} ({fmt_dt(groups[name][0])})")
    lines.append("")
    lines.append("Сохранены бэкапы:")
    labels = ["Последний", "Средний", "Ранний"]
    for name in order:
        lines.append(f"{name}:")
        for index, path in enumerate(groups[name][:3]):
            label = labels[index] if index < len(labels) else f"#{index + 1}"
            lines.append(f"  - {label}: {fmt_dt(path)} ({format_size(path.stat().st_size)})")
        lines.append("")
    total_size = sum(path.stat().st_size for path in files)
    lines.append(f"Общий размер: {format_size(total_size)}")
    return "\n".join(lines)


def split_into(path: Path, dest_dir: Path, parts: int = 2) -> list[Path]:
    """Split `path` into `parts` byte-equal chunks inside `dest_dir` (part_00, part_01, ...)."""
    subprocess.run(
        ["split", "-n", str(parts), "-d", str(path), str(dest_dir / "part_")],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(dest_dir.glob("part_*"))


def _pg_as_postgres(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a PostgreSQL client command as the postgres superuser (local peer auth)."""
    return subprocess.run(["sudo", "-u", "postgres", *args], **kwargs)


def _verify_temp_db_name(db_name: str) -> str:
    raw = f"{VERIFY_DB_PREFIX}{db_name}_{os.getpid()}_{int(time.time())}".lower()
    return re.sub(r"[^a-z0-9_]", "_", raw)[:60]


def _first_error_line(stderr: str) -> str:
    for line in stderr.splitlines():
        if "error" in line.lower():
            return line.strip()[:200]
    return ""


def _count_public_tables(db_name: str) -> int:
    result = _pg_as_postgres(
        [
            "psql",
            "-tAc",
            "select count(*) from information_schema.tables where table_schema='public'",
            "-d",
            db_name,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return int(result.stdout.strip() or 0)


def _restore_into_temp_db(db_name: str, dump: Path) -> tuple[bool, str]:
    """Restore `dump` into a throwaway database; return (ok, human-readable detail).

    Dumps that Telegram delivers split into parts (VERIFY_SPLIT_DBS) are re-split and
    re-joined exactly like the recipient does, so the artifact under test is the same
    byte stream that arrives in Telegram. The throwaway DB is always dropped.
    """
    temp_db = _verify_temp_db_name(db_name)
    split = db_name in VERIFY_SPLIT_DBS and dump.stat().st_size > TG_DOC_LIMIT
    # Same count the recipient's split produces, so the verified byte stream matches Telegram.
    parts_count = split_part_count(dump.stat().st_size) if split else 1
    started = time.monotonic()
    try:
        _pg_as_postgres(["dropdb", "--if-exists", temp_db], check=True, capture_output=True, text=True)
        _pg_as_postgres(["createdb", temp_db], check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return False, f"не удалось создать временную БД: {exc}"

    try:
        with tempfile.TemporaryDirectory(prefix="verify_restore_") as workdir:
            if split:
                parts = split_into(dump, Path(workdir), parts_count)
                with tempfile.TemporaryFile() as errfile:
                    proc = subprocess.Popen(
                        [
                            "sudo", "-u", "postgres", "env",
                            f"PGOPTIONS=-c maintenance_work_mem={VERIFY_MAINTENANCE_WORK_MEM}",
                            "pg_restore",
                            "--no-owner", "--no-privileges", "-d", temp_db,
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=errfile,
                    )
                    with proc.stdin:
                        for part in parts:
                            with part.open("rb") as handle:
                                shutil.copyfileobj(handle, proc.stdin, 1024 * 1024)
                    code = proc.wait(timeout=VERIFY_TIMEOUT_SECONDS)
                    errfile.seek(0)
                    stderr = errfile.read().decode("utf-8", "replace")
            else:
                result = subprocess.run(
                    [
                        "sudo", "-u", "postgres", "env",
                        f"PGOPTIONS=-c maintenance_work_mem={VERIFY_MAINTENANCE_WORK_MEM}",
                        "pg_restore",
                        "--no-owner", "--no-privileges", "-d", temp_db, str(dump),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=VERIFY_TIMEOUT_SECONDS,
                )
                code = result.returncode
                stderr = result.stderr
        elapsed = time.monotonic() - started
        if code != 0:
            return False, f"восстановление не удалось: {_first_error_line(stderr) or f'pg_restore код {code}'}"
        tables = _count_public_tables(temp_db)
        source = (
            f"из {parts_count} {plural_ru(parts_count, 'части', 'частей', 'частей')} (как в Telegram)"
            if split
            else "цельным файлом"
        )
        return True, f"восстановлен {source}: {tables} таблиц за {elapsed:.1f} с"
    except subprocess.TimeoutExpired:
        return False, f"таймаут восстановления (> {VERIFY_TIMEOUT_SECONDS} с)"
    except (subprocess.CalledProcessError, OSError) as exc:
        return False, f"ошибка восстановления: {exc}"
    finally:
        try:
            _pg_as_postgres(["dropdb", "--if-exists", temp_db], check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, OSError):
            pass


def verify_backups(groups: dict[str, list[Path]], order: list[str]) -> list[str]:
    """Restore the newest dump of each database into a throwaway DB and report the result."""
    lines = ["", "Проверка восстановлением (временная БД):"]
    for name in order:
        ok, detail = _restore_into_temp_db(name, groups[name][0])
        lines.append(f"  {'✅' if ok else '❌'} {name}: {detail}")
    return lines


def backup_summary(chat_id: str | None = None) -> None:
    """Bot command /backups: inventory digest plus a real restore check of each dump."""
    groups, order, files = collect_backup_groups()
    if chat_id:
        send_telegram(
            "⏳ Проверяю бэкапы восстановлением во временную БД…",
            parse_mode=None,
            chat_id=chat_id,
        )
    digest = build_backup_digest(groups, order, files)
    digest += "\n" + "\n".join(verify_backups(groups, order))
    send_long_message(digest, parse_mode=None, chat_id=chat_id)


def notify_backup(file_arg: str | None = None, dry_run: bool = False) -> None:
    """Send the freshly created dumps to the file bot and a daily report to the message bot."""
    groups, order, files = collect_backup_groups()

    # 1) Send the newest dump of each database to the file bot (split if too large).
    file_token = os.environ.get("FILE_BOT_TOKEN")
    chat_id = os.environ.get("BOT_OWNER")
    if not file_token or not chat_id:
        raise MonitorError("В .env должны быть заданы FILE_BOT_TOKEN и BOT_OWNER.")
    upload_errors: list[str] = []
    registry_entries: list[dict] = []
    for name in order:
        latest = groups[name][0]
        size = latest.stat().st_size
        sent_parts: list[tuple[Path, dict]] = []
        if size > TG_DOC_LIMIT:
            parts = split_dump(latest)
            part_sizes = [(part.name, part.stat().st_size) for part in parts]
            part_errors: list[str] = []
            try:
                for part in parts:
                    if dry_run:
                        print(f"[dry-run] sendDocument {part.name} ({format_size(part.stat().st_size)})")
                    else:
                        try:
                            sent_parts.append((part, send_document(file_token, chat_id, part)))
                        except MonitorError as exc:
                            part_errors.append(f"{part.name}: {exc}")
                if not dry_run:
                    # sha256 частей считается здесь: файлы удаляются в `finally`.
                    registry_entries.append(
                        backup_registry_entry(name, latest, chat_id, sent_parts, not part_errors)
                    )
            finally:
                for part in parts:
                    part.unlink(missing_ok=True)
            upload_errors.extend(part_errors)
            # The join instruction must accompany the split files in the file bot, every time.
            instruction = build_split_instruction(
                latest,
                part_sizes,
                None if part_errors else sha256_of(latest),
                failed=bool(part_errors),
            )
            if dry_run:
                print(f"[dry-run] sendMessage(file-bot):\n{instruction}")
            else:
                try:
                    send_telegram(instruction, parse_mode="HTML", token=file_token, chat_id=chat_id)
                except MonitorError as exc:
                    upload_errors.append(f"инструкция склейки: {exc}")
        elif dry_run:
            print(f"[dry-run] sendDocument {latest.name} ({format_size(size)})")
        else:
            try:
                sent_parts.append((latest, send_document(file_token, chat_id, latest)))
                registry_entries.append(backup_registry_entry(name, latest, chat_id, sent_parts, True))
            except MonitorError as exc:
                upload_errors.append(f"{latest.name}: {exc}")

    for entry in registry_entries:
        path = record_backup_registry(entry)
        print(f"реестр file_id: {path} ({entry['dump']}, частей {len(entry['parts'])})")

    # 2) Build the daily report for the message bot.
    message = build_backup_digest(groups, order, files)
    if upload_errors:
        message += "\n\n⚠️ Не удалось отправить файлы в бот бэкапов:"
        message += "".join(f"\n  - {err}" for err in upload_errors)

    if dry_run:
        print("[dry-run] sendMessage:\n" + message)
        return
    send_telegram(message, parse_mode=None)
    # Report failure to systemd (Result=/ExecMainStatus) — otherwise a lost upload still
    # exits 0 and the notification service looks healthy while the owner never got the dump.
    if upload_errors:
        raise MonitorError(
            f"Не доставлено файлов: {len(upload_errors)} ({'; '.join(upload_errors)})"
        )


def percent_setting(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise MonitorError(f"{name} должен быть числом от 0 до 100.") from exc
    if not 0 <= value <= 100:
        raise MonitorError(f"{name} должен быть числом от 0 до 100.")
    return value


def check_alerts() -> None:
    """Alert on disk/inode pressure and a missing or stale database backup."""
    warn_disk = percent_setting("DISK_WARN_PERCENT", 85)
    critical_disk = percent_setting("DISK_CRIT_PERCENT", 95)
    warn_inode = percent_setting("INODE_WARN_PERCENT", 85)
    critical_inode = percent_setting("INODE_CRIT_PERCENT", 95)
    if warn_disk >= critical_disk or warn_inode >= critical_inode:
        raise MonitorError("Порог warning должен быть меньше порога critical.")

    disk_path = env_path("MONITOR_DISK_PATH", "/")
    try:
        disk = shutil.disk_usage(disk_path)
        stat = os.statvfs(disk_path)
    except OSError as exc:
        raise MonitorError(f"Не удалось проверить диск {disk_path}: {exc}") from exc

    disk_percent = (disk.used / disk.total * 100) if disk.total else 0.0
    inode_total = stat.f_files
    inode_available = stat.f_favail
    inode_percent = (
        max(0, inode_total - inode_available) / inode_total * 100 if inode_total else 0.0
    )

    active: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    if disk_percent >= critical_disk:
        active["disk"] = "critical"
    elif disk_percent >= warn_disk:
        active["disk"] = "warning"
    descriptions["disk"] = (
        f"Диск {disk_path}: занято {disk_percent:.1f}% "
        f"({format_size(disk.free)} свободно)."
    )

    if inode_percent >= critical_inode:
        active["inode"] = "critical"
    elif inode_percent >= warn_inode:
        active["inode"] = "warning"
    descriptions["inode"] = f"Inode на {disk_path}: занято {inode_percent:.1f}%."

    backup_dir = env_path("BACKUP_DIR", "/var/backups")
    pattern = os.environ.get("BACKUP_PATTERN", "*.sql.gz")
    try:
        max_backup_age_hours = float(os.environ.get("MAX_BACKUP_AGE_HOURS", "26"))
    except ValueError as exc:
        raise MonitorError("MAX_BACKUP_AGE_HOURS должен быть положительным числом.") from exc
    if max_backup_age_hours <= 0:
        raise MonitorError("MAX_BACKUP_AGE_HOURS должен быть положительным числом.")

    try:
        backup_files = [path for path in backup_dir.glob(pattern) if path.is_file()]
        newest_backup = max(backup_files, key=lambda path: path.stat().st_mtime) if backup_files else None
    except OSError as exc:
        raise MonitorError(f"Не удалось проверить бэкапы в {backup_dir}: {exc}") from exc

    if newest_backup is None:
        active["backup"] = "missing"
        descriptions["backup"] = f"Не найдены файлы бэкапа в {backup_dir} по шаблону {pattern!r}."
    else:
        age_hours = max(0.0, time.time() - newest_backup.stat().st_mtime) / 3600
        if age_hours > max_backup_age_hours:
            active["backup"] = "stale"
            descriptions["backup"] = (
                f"Последний бэкап {newest_backup.name} создан {age_hours:.1f} ч назад "
                f"(порог {max_backup_age_hours:g} ч)."
            )

    alert_path = env_path(
        "MONITOR_ALERT_STATE_FILE", str(BASE_DIR / ".monitor_alert_state.json")
    )
    lock_path = alert_path.with_suffix(alert_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous_value = read_state(alert_path).get("alerts", {})
        previous: dict[str, str] = previous_value if isinstance(previous_value, dict) else {}
        messages: list[str] = []
        labels = {"disk": "💿 Диск", "inode": "📎 Inode", "backup": "💾 Бэкап БД"}
        emoji_map = {"critical": "🔴", "warning": "🟠", "missing": "🔴", "stale": "🔴"}
        
        for key, status in active.items():
            if previous.get(key) != status:
                severity_emoji = emoji_map.get(status, "⚠️")
                severity_text = "КРИТИЧНО" if status in {"critical", "missing", "stale"} else "ПРЕДУПРЕЖДЕНИЕ"
                messages.append(
                    f"{severity_emoji} <b>{severity_text}</b>: {labels[key]}\n{descriptions[key]}"
                )
        
        for key in previous.keys() - active.keys():
            if key in labels:
                messages.append(f"✅ {labels[key]}: проблема устранена.")

        if messages:
            header = f"⚠️ <b>АЛЕРТЫ СИСТЕМЫ</b>\n{'─' * 40}\n\n"
            send_telegram(header + "\n\n".join(messages), parse_mode="HTML")
        
        write_state(alert_path, {"alerts": active, "checked_at": datetime.now().astimezone().isoformat()})


def import_psutil() -> Any:
    try:
        import psutil
    except ImportError as exc:
        raise MonitorError(
            "Для сбора метрик установите зависимость: python3 -m pip install psutil"
        ) from exc
    return psutil


def format_mb_gb(size_bytes: float) -> str:
    """Форматирует байты в MB или GB (1 MB = 1024 KB, 1 GB = 1024 MB)."""
    megabytes = float(size_bytes) / (1024**2)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes:.1f} MB"


def percent_of(part: float, total: float) -> float:
    return (part / total * 100) if total else 0.0


def read_meminfo() -> dict[str, int]:
    """Читает /proc/meminfo и возвращает значения в байтах."""
    values: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            key, _, raw = line.partition(":")
            fields = raw.split()
            if not fields:
                continue
            try:
                values[key] = int(fields[0]) * 1024
            except ValueError:
                continue
    return values


def memory_snapshot() -> tuple[int, int, int]:
    """(всего, занято, свободно) в байтах; свободно — доступная память (MemAvailable)."""
    try:
        info = read_meminfo()
        total = info["MemTotal"]
        available = info.get("MemAvailable", info.get("MemFree"))
        if available is None:
            raise KeyError("MemAvailable")
        return total, max(0, total - available), available
    except (OSError, KeyError, ValueError):
        memory = import_psutil().virtual_memory()
        return memory.total, memory.used, memory.available


def cpu_ticks() -> tuple[int, int]:
    """(всего тиков, тиков простоя) для всех ядер из /proc/stat."""
    with open("/proc/stat", encoding="ascii") as handle:
        fields = handle.readline().split()
    values = [int(value) for value in fields[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_utilization(total_before: int, idle_before: int, total_after: int, idle_after: int) -> float:
    """Утилизация CPU в процентах по разнице тиков за окно выборки."""
    delta_total = total_after - total_before
    if delta_total <= 0:
        return 0.0
    delta_idle = idle_after - idle_before
    return min(100.0, max(0.0, (delta_total - delta_idle) / delta_total * 100))


def cpu_cores() -> tuple[int, int]:
    """(физических, логических) ядер; при отсутствии psutil — счёт по /proc/cpuinfo."""
    try:
        psutil = import_psutil()
        physical = psutil.cpu_count(logical=False) or 0
        logical = psutil.cpu_count(logical=True) or 0
        if logical:
            return physical or logical, logical
    except MonitorError:
        pass
    logical = os.cpu_count() or 0
    physical = 0
    try:
        with open("/proc/cpuinfo", encoding="ascii") as handle:
            lines = handle.readlines()
        physical = sum(1 for line in lines if line.startswith("physical id"))
        logical = sum(1 for line in lines if line.startswith("processor")) or logical
    except OSError:
        pass
    return physical or logical, logical


def boot_snapshot() -> tuple[str, str]:
    """Возвращает строки uptime и времени последней загрузки (UTC+3)."""
    uptime_seconds: float | None = None
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            uptime_seconds = float(handle.readline().split()[0])
    except (OSError, ValueError, IndexError):
        uptime_seconds = None
    if uptime_seconds is None:
        uptime_seconds = max(0.0, time.time() - import_psutil().boot_time())

    total_seconds = int(uptime_seconds)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    booted_at = datetime.fromtimestamp(time.time() - uptime_seconds, timezone(timedelta(hours=3)))
    return (
        f"Uptime: {days} days, {hours:02d}:{minutes:02d}:{seconds:02d}",
        f"Rebooted: {booted_at.strftime('%d.%m.%Y, %H:%M')} (UTC+3)",
    )


def process_snapshot(
    sample_seconds: float = 0.8,
) -> tuple[float, list[tuple[float, str]], list[tuple[int, str]]]:
    """За одно окно выборки снимает утилизацию CPU и топ-5 процессов по CPU и RAM."""
    psutil = import_psutil()
    processes: list[Any] = []
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)  # прайминг счётчиков CPU до окна выборки
            processes.append(proc)
        except (psutil.Error, OSError):
            continue

    try:
        total_before, idle_before = cpu_ticks()
    except (OSError, ValueError, IndexError):
        time.sleep(sample_seconds)
        cpu_percent = psutil.cpu_percent(interval=0.5)
    else:
        time.sleep(sample_seconds)
        try:
            total_after, idle_after = cpu_ticks()
            cpu_percent = cpu_utilization(total_before, idle_before, total_after, idle_after)
        except (OSError, ValueError, IndexError):
            cpu_percent = psutil.cpu_percent(interval=0.5)

    cpu_rows: list[tuple[float, str]] = []
    memory_rows: list[tuple[int, str]] = []
    for proc in processes:
        try:
            with proc.oneshot():
                name = proc.name() or f"PID {proc.pid}"
                cpu = proc.cpu_percent(None)
                rss = proc.memory_info().rss
            cpu_rows.append((max(0.0, cpu), name))
            memory_rows.append((rss, name))
        except (psutil.Error, OSError):
            continue
    cpu_rows.sort(key=lambda row: row[0], reverse=True)
    memory_rows.sort(key=lambda row: row[0], reverse=True)
    return cpu_percent, cpu_rows[:5], memory_rows[:5]


def build_health_report() -> str:
    """Собирает снапшот метрик сервера в момент вызова."""
    disk_path = env_path("MONITOR_DISK_PATH", "/")
    total_ram, used_ram, free_ram = memory_snapshot()
    try:
        disk = shutil.disk_usage(str(disk_path))
    except OSError as exc:
        raise MonitorError(f"Не удалось проверить диск {disk_path}: {exc}") from exc
    physical_cores, logical_cores = cpu_cores()
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    cpu_percent, cpu_top, memory_top = process_snapshot()
    uptime_line, reboot_line = boot_snapshot()

    lines = [
        "🖥 <b>ДАШБОРД СЕРВЕРА</b>",
        "─" * 32,
        "",
        "<b>🧠 RAM</b>",
        f"Занято: {format_mb_gb(used_ram)} | {percent_of(used_ram, total_ram):.1f}%, "
        f"Свободно: {format_mb_gb(free_ram)} | {percent_of(free_ram, total_ram):.1f}%, "
        f"Всего: {format_mb_gb(total_ram)}",
        "",
        "<b>⚙️ CPU</b>",
        f"Количество ядер: {physical_cores} физических / {logical_cores} логических",
    ]
    if load:
        lines.append(f"Load Average: {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}")
    lines += [
        f"Текущая утилизация: {cpu_percent:.1f}%",
        "",
        f"<b>💿 Диск {html.escape(str(disk_path))}</b>",
        f"Занято: {format_mb_gb(disk.used)} | {percent_of(disk.used, disk.total):.1f}%, "
        f"Свободно: {format_mb_gb(disk.free)} | {percent_of(disk.free, disk.total):.1f}%, "
        f"Всего: {format_mb_gb(disk.total)}",
        "",
        uptime_line,
        reboot_line,
        "",
        "<b>💾 Топ-5 процессов по RAM:</b>",
    ]
    for index, (rss, name) in enumerate(memory_top, start=1):
        lines.append(
            f"{index}. {html.escape(name)} — {format_mb_gb(rss)} | {percent_of(rss, total_ram):.1f}%"
        )
    lines.append("")
    lines.append("<b>⚡️ Топ-5 процессов по CPU:</b>")
    for index, (cpu, name) in enumerate(cpu_top, start=1):
        lines.append(f"{index}. {html.escape(name)} — {cpu:.1f}%")
    return "\n".join(lines)


def server_health(chat_id: str | None = None) -> None:
    send_long_message(build_health_report(), parse_mode="HTML", chat_id=chat_id)


def systemd_result(unit: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p", "ActiveEnterTimestamp"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        return f"не удалось получить статус: {exc}"
    return ", ".join(part for part in out.strip().splitlines() if part)


def verify_backup() -> None:
    """Check the last nightly backup run and send a report to the message bot."""
    backup_dir = env_path("BACKUP_DIR", "/var/backups").resolve()
    pattern = os.environ.get("BACKUP_PATTERN", "*.dump")
    now = time.time()
    lines = ["🔎 <b>ПРОВЕРКА НОЧНОГО БЭКАПА</b>", "─" * 40]
    problems: list[str] = []

    log_file = backup_dir / "backup.log"
    if log_file.is_file():
        tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        last_start = None
        for index, line in enumerate(tail):
            if "Starting database backup" in line:
                last_start = index
        block = tail[last_start:] if last_start is not None else tail
        ok = any("completed successfully" in line for line in block)
        failed = [line for line in block if "✗" in line or "failed" in line.lower()]
        lines.append(f"<b>Журнал бэкапа:</b> {'✅ успешно' if ok else '❌ не завершён успешно'}")
        if failed:
            problems.append("в журнале бэкапа есть ошибки")
            lines.extend(f"  {line}" for line in failed[:5])
    else:
        problems.append("нет backup.log")
        lines.append("<b>Журнал бэкапа:</b> ❌ не найден")

    files = sorted(
        (path for path in backup_dir.glob(pattern) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    groups: dict[str, list[Path]] = {}
    for path in files:
        groups.setdefault(db_name_from_file(path), []).append(path)

    lines.append("")
    lines.append("<b>Дампы:</b>")
    for name in sorted(groups):
        newest = groups[name][0]
        age_hours = (now - newest.stat().st_mtime) / 3600
        fresh = age_hours <= 26
        if not fresh:
            problems.append(f"{name}: свежий дамп старше 26 ч")
        lines.append(
            f"  {'✅' if fresh else '❌'} {name}: {len(groups[name])} шт., "
            f"последний {newest.name} ({age_hours:.1f} ч назад)"
        )

    lines.append("")
    lines.append("<b>Восстановление (во временную БД):</b>")
    for name in sorted(groups):
        restored, detail = _restore_into_temp_db(name, groups[name][0])
        lines.append(f"  {'✅' if restored else '❌'} {name}: {detail}")
        if not restored:
            problems.append(f"{name}: {detail}")

    lines.append("")
    lines.append("<b>Сервисы:</b>")
    for unit in ("postgres-backup.service", "postgres-backup-notify.service"):
        result = systemd_result(unit)
        lines.append(f"  {unit}: {result}")
        if "Result=success" not in result:
            problems.append(f"{unit}: {result}")

    lines.append("")
    if problems:
        lines.append("⚠️ <b>Проблемы:</b>")
        lines.extend(f"  - {problem}" for problem in problems)
    else:
        lines.append("✅ Всё в порядке: дампы на месте и восстановлены во временную БД.")
    send_long_message("\n".join(lines), parse_mode="HTML")


def send_text(text: str) -> None:
    """Send an arbitrary message to the message bot."""
    send_telegram(text, parse_mode=None)


BOT_HELP = (
    "🤖 <b>Серверный монитор</b>\n"
    "\n"
    "/health — снапшот метрик сервера (RAM, CPU, диск, uptime, топ процессов)\n"
    "/healthcheck — проверка работоспособности бота (ответ 200, статус ок)\n"
    "/docker_ps — сводка по Docker-контейнерам (статус и uptime/downtime)\n"
    "/backups — дайджест последнего бэкапа БД (результат ночного прогона)\n"
    "/help — эта справка"
)


def telegram_api(
    token: str, method: str, params: dict[str, Any] | None = None, timeout: int = 40
) -> Any:
    """POST a request to the Telegram Bot API and return its `result`."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    payload = urlencode(params or {}).encode("utf-8")
    request = Request(url, data=payload, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            error = str(error_body.get("description") or exc)
        except (UnicodeDecodeError, json.JSONDecodeError):
            error = str(exc)
        raise MonitorError(f"Telegram API {method} HTTP {exc.code}: {error.replace(token, '<redacted>')}") from exc
    except Exception as exc:  # noqa: BLE001 - timeouts and network failures are retried by the caller
        raise MonitorError(
            f"Telegram API {method}: {str(exc).replace(token, '<redacted>')}"
        ) from exc
    if not body.get("ok"):
        raise MonitorError(f"Telegram API {method} вернул ошибку: {body}")
    return body.get("result")


def bot_state_file() -> Path:
    return env_path("MONITOR_BOT_STATE_FILE", "/var/lib/server-monitor/bot_state.json")


def bot_healthcheck(chat_id: str | None = None) -> None:
    """Liveness probe for the bot itself.

    The reply travels through the Telegram Bot API, so a delivered answer proves
    the long-polling loop is alive and can reach Telegram. External systems
    (Docker healthcheck, uptime monitors) treat the fixed 200/ок answer as OK.
    """
    send_telegram("200. Статус: ок", parse_mode=None, chat_id=chat_id)


def handle_bot_message(chat_id: str, text: str) -> None:
    command = text.split()[0].split("@")[0].lower() if text.split() else ""
    if command == "/health":
        server_health(chat_id=chat_id)
    elif command == "/healthcheck":
        bot_healthcheck(chat_id=chat_id)
    elif command == "/docker_ps":
        docker_ps(chat_id=chat_id)
    elif command == "/backups":
        backup_summary(chat_id=chat_id)
    elif command in ("/start", "/help"):
        send_telegram(BOT_HELP, parse_mode="HTML", chat_id=chat_id)
    else:
        send_telegram(
            "Неизвестная команда. Доступно: /health, /healthcheck, /docker_ps, /backups, /help.",
            parse_mode=None,
            chat_id=chat_id,
        )


def run_bot() -> None:
    """Long-poll Telegram and answer owner commands (/health, /docker_ps, /backups)."""
    token = os.environ.get("BOT_TOKEN")
    owner = str(os.environ.get("BOT_OWNER") or "").strip()
    if not token or not owner:
        raise MonitorError("В .env должны быть заданы BOT_TOKEN и BOT_OWNER (Telegram chat ID).")

    state_path = bot_state_file()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("bot: другой экземпляр уже опрашивает Telegram, выход.", flush=True)
            return

        state = read_state(state_path)
        offset = state.get("offset")
        if not isinstance(offset, int):
            # Первый запуск: пропускаем накопившийся бэклог, отвечаем только на новые команды.
            offset = 0
            try:
                backlog = telegram_api(token, "getUpdates", {"offset": -1, "timeout": 0}, timeout=20)
                if backlog:
                    offset = int(backlog[-1]["update_id"]) + 1
            except MonitorError as exc:
                print(f"bot: не удалось прочитать бэклог: {exc}", file=sys.stderr, flush=True)
        write_state(state_path, {"offset": offset, "started_at": datetime.now().astimezone().isoformat()})
        print(f"bot: запущен, owner={owner}, offset={offset}", flush=True)

        while True:
            try:
                updates = telegram_api(
                    token,
                    "getUpdates",
                    {"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
                    timeout=45,
                )
            except MonitorError as exc:
                print(f"bot: {exc}", file=sys.stderr, flush=True)
                time.sleep(5)
                continue
            for update in updates or []:
                offset = int(update["update_id"]) + 1
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", "")).strip()
                text = (message.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if chat_id != owner:
                    print(f"bot: команда от {chat_id} отклонена (не владелец).", flush=True)
                    continue
                try:
                    handle_bot_message(chat_id, text)
                except MonitorError as exc:
                    print(f"bot: {exc}", file=sys.stderr, flush=True)
                    try:
                        send_telegram(f"⚠️ Ошибка: {exc}", parse_mode=None, chat_id=chat_id)
                    except MonitorError:
                        pass
            try:
                write_state(state_path, {"offset": offset})
            except OSError as exc:
                print(f"bot: не удалось сохранить offset: {exc}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "events", help="наблюдать за событиями Docker (start/stop/die/restart/oom)"
    )
    subparsers.add_parser("health", help="отправить отчёт о состоянии сервера")
    subparsers.add_parser("bot", help="запустить интерактивный бот-опросчик Telegram (/health, /docker_ps, /backups)")
    subparsers.add_parser("alerts", help="проверить пороги диска/inode и свежесть бэкапа")
    backup_parser = subparsers.add_parser("backup", help="отправить отчёт о готовом бэкапе")
    backup_parser.add_argument("file", nargs="?", help="созданный файл бэкапа; если не указан — самый новый")
    backup_parser.add_argument(
        "--dry-run", action="store_true", help="показать план отправки, не обращаясь к Telegram"
    )
    subparsers.add_parser("verify", help="проверить ночной бэкап и отправить отчёт в бот уведомлений")
    send_parser = subparsers.add_parser("send", help="отправить произвольный текст в бот уведомлений")
    send_parser.add_argument("text", help="текст сообщения")
    args = parser.parse_args()

    try:
        if args.command == "events":
            watch_docker_events()
        elif args.command == "health":
            server_health()
        elif args.command == "bot":
            run_bot()
        elif args.command == "alerts":
            check_alerts()
        elif args.command == "backup":
            notify_backup(args.file, dry_run=args.dry_run)
        elif args.command == "verify":
            verify_backup()
        elif args.command == "send":
            send_text(args.text)
    except MonitorError as exc:
        print(f"monitor: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
