#!/usr/bin/env python3
"""
sync_assets.py — подтягивает живые файлы хоста в assets/ репозитория.

Зачем: репозиторий должен отражать то, что реально работает, а не то, что
кто-то когда-то скопировал руками. Скрипт сравнивает содержимое и пишет
только реально изменившиеся файлы (бинарно-безопасно), поэтому повторный
запуск — no-op.

Секреты исключены жёстко: .env, __pycache__, снапшоты состояния, out/,
тесты мониторинга (в них встречаются реальные chat_id).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOST_STACK = Path("/home/ai_service/ai-stack/ai")
HOST_NOTIF = Path("/home/ai_service/notifications")
HOST_BIN = Path("/usr/local/bin")
HOST_UNITS = Path("/etc/systemd/system")
HOST_OTC = Path("/root/open-terminal-config")

STACK_EXCLUDE_DIRS = {"_state_snapshot", "out", "__pycache__", ".git"}
STACK_EXCLUDE_FILES = {".env"}
NOTIF_EXCLUDE_DIRS = {"__pycache__", ".git"}
NOTIF_EXCLUDE_FILES = {".env", ".gitignore"}
NOTIF_EXCLUDE_GLOBS = ("test_*.py", "*.bak*")


def copy_if_changed(src: Path, dst: Path) -> bool:
    try:
        blob = src.read_bytes()
    except OSError:
        return False
    if dst.is_file():
        try:
            if dst.read_bytes() == blob:
                return False
        except OSError:
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".tmp-{dst.name}")
    tmp.write_bytes(blob)
    os.chmod(tmp, 0o755 if os.access(src, os.X_OK) else 0o644)
    os.replace(tmp, dst)
    return True


def sync_tree(src: Path, dst: Path, exclude_dirs: set, exclude_files: set, globs: tuple = ()) -> int:
    if not src.is_dir():
        return 0
    changed = 0
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if item.name in exclude_files:
            continue
        if any(item.match(g) for g in globs):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if copy_if_changed(item, target):
            changed += 1
            print(f"  ~ {target.relative_to(REPO)}")
    return changed


def sync_managed(src_dir: Path, dst_dir: Path) -> int:
    """Копирует только те файлы, которые уже есть в репозитории (манифест)."""
    if not dst_dir.is_dir():
        return 0
    changed = 0
    for target in sorted(p for p in dst_dir.iterdir() if p.is_file()):
        src = src_dir / target.name
        if src.is_file() and copy_if_changed(src, target):
            changed += 1
            print(f"  ~ {target.relative_to(REPO)}")
    return changed


def main() -> int:
    total = 0
    total += sync_tree(HOST_STACK, REPO / "assets" / "stack", STACK_EXCLUDE_DIRS, STACK_EXCLUDE_FILES)
    total += sync_tree(HOST_NOTIF, REPO / "assets" / "notifications", NOTIF_EXCLUDE_DIRS, NOTIF_EXCLUDE_FILES,
                       NOTIF_EXCLUDE_GLOBS)
    total += sync_managed(HOST_BIN, REPO / "assets" / "host" / "bin")
    total += sync_managed(HOST_UNITS, REPO / "assets" / "host" / "systemd")
    total += sync_managed(HOST_OTC, REPO / "assets" / "host" / "open-terminal-config")
    print(f"sync: {total} file(s) changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
