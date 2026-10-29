#!/usr/bin/env python3
"""Weekly offsite backup of Open WebUI user files to the file bot.

What is backed up: `uploads/` (Knowledge sources, chat attachments) and
`generated_images/`. `cache/` is skipped — it is regenerable RAG cache.

The archive is split into parts that fit Telegram's 20 MB getFile limit and every
part's file_id lands in the same registry (`tg-registry.json`) install.py reads,
under the pseudo-DB name `webui_data`. DB dumps keep their own entries.

Restore: install.py --from-bot (if it sees a webui_data set) or
/usr/local/bin/webui-data-restore.py.
"""

from __future__ import annotations

import os
import sys
import time
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ai_service/notifications")
import monitor  # noqa: E402  (reuses send_document/split/registry, loads .env on import)

SRC = Path("/home/ai_service/webui_data")
INCLUDE = ("uploads", "generated_images")
WORK = Path("/var/lib/webui-data-backup")
KEEP_ARCHIVES = 2
DB_NAME = "webui_data"
PREFIX = "webui_data_files"
UPLOAD_RETRIES = 3


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def build_archive() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = WORK / f"{PREFIX}_{stamp}.tar.gz"
    log(f"archiving {', '.join(INCLUDE)} → {archive.name}")
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for name in INCLUDE:
            path = SRC / name
            if path.exists():
                tar.add(path, arcname=name)
    log(f"archive ready: {monitor.format_size(archive.stat().st_size)}")
    return archive


def prune_archives() -> None:
    archives = sorted(
        WORK.glob(f"{PREFIX}_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True
    )
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
                message = str(exc)
                if attempt == UPLOAD_RETRIES:
                    errors.append(f"{part.name}: {message}")
                    log(f"  {index}/{len(parts)} ✗ {message}")
                else:
                    delay = 5 * attempt
                    log(f"  {index}/{len(parts)} retry in {delay}s: {message}")
                    time.sleep(delay)
        time.sleep(0.5)  # stay under the per-chat flood limit
    return sent, parts, errors


def main() -> int:
    token = os.environ.get("FILE_BOT_TOKEN")
    chat_id = os.environ.get("BOT_OWNER")
    if not token or not chat_id:
        log("FILE_BOT_TOKEN/BOT_OWNER missing in notifications/.env")
        return 1

    archive = build_archive()
    sent, parts, errors = upload(archive, token, chat_id)
    part_sizes = [(part.name, part.stat().st_size) for part in parts]

    entry = monitor.backup_registry_entry(DB_NAME, archive, chat_id, sent, not errors)
    registry_path = monitor.record_backup_registry(entry)
    log(f"registry: {registry_path} ({len(sent)} parts recorded)")
    for part in parts:
        part.unlink(missing_ok=True)

    if errors:
        text = "🔴 <b>Бэкап файлов WebUI неполный</b>\n" + "\n".join(errors[:5])
        monitor.send_telegram(text, parse_mode="HTML")
        return 1

    digest = monitor.sha256_of(archive)
    if len(part_sizes) <= 10:
        instruction = monitor.build_split_instruction(archive, part_sizes, digest, failed=False)
    else:
        # 55 частей не влезают в 4096 символов, которые принимает Telegram.
        instruction = (
            f"<b>{archive.name}</b> — {len(part_sizes)} частей по 18 MB\n"
            f"Склейка (порядок по именам):\n"
            f"<code>cat {archive.name}.part_* &gt; {archive.name}</code>\n"
            f"SHA-256: <code>{digest}</code>"
        )
    monitor.send_telegram(instruction, parse_mode="HTML", token=token, chat_id=chat_id)
    monitor.send_telegram(
        "💾 <b>Бэкап файлов WebUI</b>\n"
        f"{archive.name}\n"
        f"{monitor.format_size(archive.stat().st_size)} · {len(sent)} частей · sha256 ok\n"
        f"Скачать обратно: install.py --from-bot",
        parse_mode="HTML",
    )
    prune_archives()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
