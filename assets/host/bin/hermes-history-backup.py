#!/usr/bin/env python3
"""Weekly offsite backup of the Hermes *history* (what hermes-state does not carry).

`hermes-state-backup.sh` pushes config, memories, skills, cron, plugins/hooks/scripts to
the private GitHub repo — but deliberately not `state.db` (conversations) and not
`sessions/`. After a total host loss those would be gone. This job closes that gap:
a consistent SQLite snapshot of `state.db` + `kanban.db` + `sessions/` goes to the
private Telegram file bot, same channel and same split/registry machinery as the
Open WebUI files backup.

Restore: pull the archive from the bot, `tar xzf`, drop the files back into
`/root/.hermes/` with the agent stopped.

Usage:
    hermes-history-backup.py [--dry-run] [--no-send]
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ai_service/notifications")
import monitor  # noqa: E402  (reuses send_document/split/registry, loads .env on import)

HERMES_HOME = Path("/root/.hermes")
SQLITE_FILES = ("state.db", "kanban.db")
DIRS = ("sessions",)
WORK = Path("/var/lib/hermes-history-backup")
KEEP_ARCHIVES = 2
DB_NAME = "hermes_history"
PREFIX = "hermes_history"
UPLOAD_RETRIES = 3


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def snapshot_db(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite DB (WAL-safe), not a byte copy."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def build_archive() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    os.chmod(WORK, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = WORK / f"{PREFIX}_{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="hermes_history_") as tmp:
        staged = Path(tmp)
        for name in SQLITE_FILES:
            src = HERMES_HOME / name
            if not src.exists():
                log(f"skip missing {name}")
                continue
            snapshot_db(src, staged / name)
            log(f"snapshot {name}: {monitor.format_size((staged / name).stat().st_size)}")
        for name in DIRS:
            src = HERMES_HOME / name
            if src.exists():
                # symlink-free copy: sessions may hold odd entries
                staged.joinpath(name).mkdir(exist_ok=True)
                for item in src.rglob("*"):
                    rel = item.relative_to(src)
                    target = staged / name / rel
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif item.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(item.read_bytes())
        log(f"archiving → {archive.name}")
        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            for entry in sorted(staged.iterdir()):
                tar.add(entry, arcname=entry.name)
    os.chmod(archive, 0o600)
    log(f"archive ready: {monitor.format_size(archive.stat().st_size)}")
    return archive


def prune_archives() -> None:
    archives = sorted(WORK.glob(f"{PREFIX}_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[KEEP_ARCHIVES:]:
        old.unlink(missing_ok=True)
        log(f"pruned local {old.name}")


def upload(archive: Path, token: str, chat_id: str) -> tuple[list, list[Path], list[str]]:
    parts = monitor.split_dump(archive)
    log(f"{len(parts)} parts ≤ {monitor.format_size(monitor.TG_DOC_LIMIT)}")
    sent: list[tuple[Path, dict]] = []
    errors: list[str] = []
    for index, part in enumerate(parts, 1):
        for attempt in range(1, UPLOAD_RETRIES + 1):
            try:
                sent.append((part, monitor.send_document(token, chat_id, part)))
                log(f"  {index}/{len(parts)} ✓ {part.name}")
                break
            except monitor.MonitorError as exc:
                if attempt == UPLOAD_RETRIES:
                    errors.append(f"{part.name}: {exc}")
                    log(f"  {index}/{len(parts)} ✗ {exc}")
                else:
                    delay = 5 * attempt
                    log(f"  {index}/{len(parts)} retry in {delay}s: {exc}")
                    time.sleep(delay)
        time.sleep(0.5)
    return sent, parts, errors


def main() -> int:
    dry_run = "--dry-run" in sys.argv or "--no-send" in sys.argv
    token = os.environ.get("FILE_BOT_TOKEN")
    chat_id = os.environ.get("BOT_OWNER")
    if not dry_run and (not token or not chat_id):
        log("FILE_BOT_TOKEN/BOT_OWNER missing in notifications/.env")
        return 1

    archive = build_archive()
    if dry_run:
        log(f"dry-run: not sending {archive.name}")
        return 0

    sent, parts, errors = upload(archive, token, chat_id)
    part_sizes = [(part.name, part.stat().st_size) for part in parts]
    entry = monitor.backup_registry_entry(DB_NAME, archive, chat_id, sent, not errors)
    monitor.record_backup_registry(entry)
    for part in parts:
        part.unlink(missing_ok=True)

    if errors:
        monitor.send_telegram(
            "🔴 <b>Бэкап истории Hermes неполный</b>\n" + "\n".join(errors[:5]),
            parse_mode="HTML", token=token, chat_id=chat_id,
        )
        return 1

    digest = monitor.sha256_of(archive)
    monitor.send_telegram(
        "🧠 <b>Бэкап истории Hermes</b> (state.db + sessions)\n"
        f"{archive.name} · {monitor.format_size(archive.stat().st_size)} · {len(sent)} частей\n"
        f"<code>cat {archive.name}.part_* &gt; {archive.name}</code>\n"
        f"SHA-256: <code>{digest}</code>",
        parse_mode="HTML",
        token=token,
        chat_id=chat_id,
    )
    prune_archives()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
