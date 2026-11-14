#!/usr/bin/env python3
"""Weekly offsite backup of Open WebUI user files to the file bot.

What is backed up:
  * `webui_data/uploads/` — Knowledge sources and chat attachments (uploads still
    land in the shared volume, the sandbox does not touch them);
  * `webui_data/generated_images/` — legacy shared image archive (frozen: the image
    tool now writes into per-user workspaces, see below);
  * per-user sandbox workspaces `sandbox/users/<slug>/` — only user artifacts:
    documents, notes, code, images, `.agents` memory. Package trees, binaries and
    git repositories are filtered out (see SKIP_* below): they are reproducible
    (pip/npm/apt/cargo) or belong in git, not in a Telegram archive.

`cache/` is skipped — it is regenerable RAG cache.

The archive is split into parts that fit Telegram's 20 MB getFile limit and every
part's file_id lands in the same registry (`tg-registry.json`) install.py reads,
under the pseudo-DB name `webui_data`. DB dumps keep their own entries.

Restore: install.py --from-bot (if it sees a webui_data set) or
/usr/local/bin/webui-data-restore.py.

Usage:
  webui-data-backup.py --dry-run   # build the archive, print the inventory, upload nothing
"""

from __future__ import annotations

import argparse
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
SANDBOX_USERS = Path("/home/ai_service/sandbox/users")
SANDBOX_PREFIX = "sandbox_users"
WORK = Path("/var/lib/webui-data-backup")
KEEP_ARCHIVES = 2
DB_NAME = "webui_data"
PREFIX = "webui_data_files"
UPLOAD_RETRIES = 3

# Каталоги, которые не бэкапим: пакетные/системные (восстанавливаются установкой),
# рантайм-кэши (мусор) и рабочие копии репозиториев (им место в git, а не в архиве).
SKIP_DIR_NAMES = frozenset({
    ".local", "bin", "venv", ".venv", "site-packages", "node_modules", ".npm",
    ".cache", ".cargo", ".rustup", ".conda", "go", "pkg", ".arduino15", "Arduino",
    "ipatool", ".tmp", ".open-webui", ".config", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".git", "dist", "build",
})
SKIP_EXT = frozenset({
    ".so", ".a", ".o", ".whl", ".deb", ".rpm", ".img", ".iso", ".apk", ".pyc",
    ".exe", ".dll", ".dylib", ".bundle", ".asar", ".wasm",
    ".tar", ".gz", ".tgz", ".xz", ".zst", ".7z",
})
MAX_FILE_BYTES = 50 * 1024 * 1024


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def _is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def artifact_filter(base: Path, stats: dict):
    """tarfile-фильтр: оставить артефакты, выкинуть пакеты, бинарники и репозитории.

    ``base`` — каталог пользователя на хосте; ``ti.name`` приходит уже с
    префиксом sandbox_users/<slug>/, поэтому хост-путь собирается как base/<rel>.
    Возврат None исключает элемент; для каталога — вместе с поддеревом.
    """

    def _filter(ti: tarfile.TarInfo):
        rel = ti.name.split("/", 2)[2] if ti.name.count("/") >= 2 else ""
        host = base / rel if rel else base
        name = Path(rel).name
        if ti.isdir():
            if name in SKIP_DIR_NAMES:
                stats["skipped_dirs"] += 1
                return None
            if (host / ".git").is_dir():
                stats["skipped_repos"] += 1
                log(f"  repo skipped: {host}")
                return None
            return ti
        if ti.isfile():
            if name in SKIP_DIR_NAMES or Path(name).suffix.lower() in SKIP_EXT:
                stats["skipped_bins"] += 1
                return None
            if ti.size > MAX_FILE_BYTES:
                stats["skipped_big"] += 1
                log(f"  big file skipped: {host} ({ti.size / 1024**2:.1f} MB)")
                return None
            if ti.size and _is_elf(host):
                stats["skipped_bins"] += 1
                return None
            stats["files"] += 1
            stats["bytes"] += ti.size
        return ti

    return _filter


def add_sandbox_users(tar: tarfile.TarFile) -> dict:
    total = {"files": 0, "bytes": 0, "skipped_dirs": 0, "skipped_bins": 0,
             "skipped_big": 0, "skipped_repos": 0, "users": 0}
    if not SANDBOX_USERS.is_dir():
        log(f"sandbox users dir missing: {SANDBOX_USERS}")
        return total
    for user_dir in sorted(p for p in SANDBOX_USERS.iterdir() if p.is_dir()):
        stats = {"files": 0, "bytes": 0, "skipped_dirs": 0, "skipped_bins": 0,
                 "skipped_big": 0, "skipped_repos": 0}
        tar.add(
            user_dir,
            arcname=f"{SANDBOX_PREFIX}/{user_dir.name}",
            filter=artifact_filter(user_dir, stats),
            recursive=True,
        )
        for key in stats:
            total[key] += stats[key]
        total["users"] += 1
        log(
            f"  {user_dir.name}: {stats['files']} artifacts "
            f"{monitor.format_size(stats['bytes'])}"
        )
    return total


def build_archive() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = WORK / f"{PREFIX}_{stamp}.tar.gz"
    log(f"archiving {', '.join(INCLUDE)} + sandbox user artifacts → {archive.name}")
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for name in INCLUDE:
            path = SRC / name
            if path.exists():
                tar.add(path, arcname=name)
        stats = add_sandbox_users(tar)
        log(
            f"sandbox users: {stats['users']} workspaces, {stats['files']} artifacts "
            f"{monitor.format_size(stats['bytes'])} (skipped: {stats['skipped_dirs']} pkg dirs, "
            f"{stats['skipped_bins']} binaries, {stats['skipped_big']} big files, "
            f"{stats['skipped_repos']} repos)"
        )
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
    parser = argparse.ArgumentParser(description="Weekly offsite backup of WebUI user files.")
    parser.add_argument("--dry-run", action="store_true",
                        help="собрать архив и показать опись, ничего не отправлять")
    args = parser.parse_args()

    token = os.environ.get("FILE_BOT_TOKEN")
    chat_id = os.environ.get("BOT_OWNER")
    if not token or not chat_id:
        log("FILE_BOT_TOKEN/BOT_OWNER missing in notifications/.env")
        return 1

    archive = build_archive()
    if args.dry_run:
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        users = sorted({n.split("/")[1] for n in names if n.startswith(SANDBOX_PREFIX + "/")})
        log(f"[dry-run] {len(names)} entries; sandbox users: {', '.join(users) or '—'}")
        log(f"[dry-run] archive kept at {archive} — nothing uploaded")
        return 0

    sent, parts, errors = upload(archive, token, chat_id)
    part_sizes = [(part.name, part.stat().st_size) for part in parts]

    entry = monitor.backup_registry_entry(DB_NAME, archive, chat_id, sent, not errors)
    registry_path = monitor.record_backup_registry(entry)
    log(f"registry: {registry_path} ({len(sent)} parts recorded)")
    for part in parts:
        part.unlink(missing_ok=True)

    if errors:
        text = "🔴 <b>Бэкап файлов WebUI неполный</b>\n" + "\n".join(errors[:5])
        monitor.send_telegram(text, parse_mode="HTML", token=token, chat_id=chat_id)
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
        token=token,
        chat_id=chat_id,
    )
    prune_archives()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
