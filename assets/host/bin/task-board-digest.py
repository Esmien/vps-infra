#!/usr/bin/env python3
"""Доска задач владельца в Telegram: одно живое сообщение, которое редактируется.

Разделение (правило владельца): **описания задач и инструкции — в харнессе, всё, что работает
на сервере, — в инфре**. Отсюда:
  доска (документ):  /root/.hermes/tasks/backlog.md
  этот скрипт:       /usr/local/bin/task-board-digest.py (источник — vps-infra/assets/host/bin)
  токен бота:        /etc/vps-infra/task-board.env (600, в офсайт-архиве секретов)
  указатель сообщения: /var/lib/hermes-task-board/message.json

Правило работы: правка задачи — это РЕДАКТИРОВАНИЕ живого сообщения. Новое сообщение
появляется только понедельничной сводкой; оно же становится живой доской.

  task-board-digest.py digest     отправить сводку НОВЫМ сообщением (таймер, понедельник)
  task-board-digest.py refresh    обновить живую доску на месте (fallback — отправить новую)
  task-board-digest.py preview    напечатать сводку
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BOARD = pathlib.Path("/root/.hermes/tasks/backlog.md")
ENV_FILE = pathlib.Path("/etc/vps-infra/task-board.env")
POINTER = pathlib.Path("/var/lib/hermes-task-board/message.json")
MSK = dt.timezone(dt.timedelta(hours=3))

# ◻️ todo · ✏️ in progress · ✅ done · ❌ declined/broken/rejected/deleted
STATUS_ORDER = ["in_progress", "todo", "done", "declined"]
STATUS_EMOJI = {"todo": "◻️", "in_progress": "✏️", "done": "✅", "declined": "❌"}
ALIASES = {"pending": "todo", "blocked": "declined", "broken": "declined", "rejected": "declined",
           "deleted": "declined", "cancelled": "declined", "canceled": "declined"}
REEDIT_ERRORS = ("message to edit not found", "message can't be edited", "message_id_invalid")


def load_env() -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values["TASK_BOT_TOKEN"], values.get("OWNER_CHAT_ID", "")


def parse_backlog() -> list[dict]:
    tasks: list[dict] = []
    current: dict | None = None
    for raw in BOARD.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^##\s+#(\d+)\s+(.*)$", line)
        if m:
            current = {"id": m.group(1), "title": m.group(2).strip(), "status": "todo", "file": ""}
            tasks.append(current)
            continue
        if current is None or not line or line.startswith("#") or line.startswith("<!--"):
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key, val = key.strip().lower(), val.strip()
        if key == "status":
            val = ALIASES.get(val.lower(), val.lower())
            current["status"] = val if val in STATUS_EMOJI else "todo"
        elif key == "file":
            current["file"] = val
    return tasks


def fmt_task(task: dict) -> str:
    out = f"{STATUS_EMOJI.get(task['status'], '◻️')} <b>#{html.escape(task['id'])}</b> {html.escape(task['title'])}"
    if task["file"]:
        out += f"\n<code>{html.escape(task['file'])}</code>"
    return out


def build_digest(tasks: list[dict]) -> str:
    now = dt.datetime.now(MSK)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    lines = [f"🗂 <b>Задачи</b> · {now.day} {months[now.month - 1]}", ""]
    for status in STATUS_ORDER:
        for task in (t for t in tasks if t["status"] == status):
            lines += [fmt_task(task), ""]
    if not tasks:
        lines.append("Пусто.")
    return "\n".join(lines).strip()


def api(method: str, **params) -> dict:
    token, _ = load_env()
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {exc.code}"}


def read_pointer() -> dict | None:
    try:
        return json.loads(POINTER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_pointer(chat_id: str, message_id: int) -> None:
    POINTER.parent.mkdir(parents=True, exist_ok=True)
    POINTER.write_text(json.dumps({"chat_id": chat_id, "message_id": message_id}), encoding="utf-8")


def send_new(text: str) -> dict:
    _, chat_id = load_env()
    if not chat_id:
        raise SystemExit("OWNER_CHAT_ID не задан в /etc/vps-infra/task-board.env")
    payload = api("sendMessage", chat_id=chat_id, text=text,
                  parse_mode="HTML", disable_web_page_preview="true")
    message_id = payload.get("result", {}).get("message_id")
    if payload.get("ok") and message_id:
        write_pointer(chat_id, message_id)
    return {"ok": payload.get("ok"), "message_id": message_id, "action": "sent"}


def update_board() -> dict:
    """Правит живое сообщение; если править нечего — отправляет новое и запоминает его."""
    text = build_digest(parse_backlog())
    pointer = read_pointer()
    if not pointer:
        return send_new(text)
    payload = api("editMessageText", chat_id=pointer["chat_id"], message_id=pointer["message_id"],
                  text=text, parse_mode="HTML", disable_web_page_preview="true")
    if payload.get("ok"):
        return {"ok": True, "message_id": pointer["message_id"], "action": "edited"}
    description = str(payload.get("description", "")).lower()
    if "not modified" in description:
        return {"ok": True, "message_id": pointer["message_id"], "action": "unchanged"}
    if any(marker in description for marker in REEDIT_ERRORS):
        result = send_new(text)
        result["action"] = "resent"
        return result
    return {"ok": False, "description": payload.get("description"), "action": "failed"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Доска задач владельца")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("digest", help="отправить сводку новым сообщением (понедельник)")
    sub.add_parser("refresh", help="обновить живую доску на месте")
    sub.add_parser("preview", help="напечатать сводку")
    args = parser.parse_args(argv)

    if args.cmd == "preview":
        print(build_digest(parse_backlog()))
        return 0
    if args.cmd == "digest":
        print(json.dumps(send_new(build_digest(parse_backlog())), ensure_ascii=False))
        return 0
    print(json.dumps(update_board(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
