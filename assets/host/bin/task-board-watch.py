#!/usr/bin/env python3
"""Сторож доски задач: ловит изменения backlog.md и ПРАВИТ живое сообщение в Telegram.

Разделение (правило владельца): описания задач — в харнессе (`/root/.hermes/tasks/backlog.md`),
а этот демон и его юнит — инфра (`/usr/local/bin/task-board-watch.py`,
`vps-infra/assets/host/{bin,systemd}`).

Path-юнит `hermes-task-board-watch` дёргает скрипт на каждое изменение файла; скрипт сравнивает
доску со снимком и, если что-то поменялось, вызывает `task-board-digest.py refresh` — то есть
редактирует живое сообщение, а не отправляет новое. Первый запуск пишет baseline молча.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

SENDER = pathlib.Path("/usr/local/bin/task-board-digest.py")
STATE = pathlib.Path("/var/lib/hermes-task-board/state.json")

spec = importlib.util.spec_from_file_location("board_digest", SENDER)
digest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digest)  # type: ignore[union-attr]


def snapshot() -> dict[str, dict]:
    return {t["id"]: {"status": t["status"], "title": t["title"], "file": t["file"]}
            for t in digest.parse_backlog()}


def load_previous() -> dict[str, dict] | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(current: dict[str, dict]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    current = snapshot()
    previous = load_previous()
    if previous is None:
        save(current)
        print("baseline written:", len(current))
        return 0
    if previous == current:
        print("no changes")
        return 0

    result = digest.update_board()
    save(current)
    print("board updated:", json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
