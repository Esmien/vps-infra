#!/usr/bin/env python3
"""Restore Open WebUI files (uploads + generated images + sandbox user artifacts)
from the Telegram file bot.

Reads the newest `webui_data` set from the tg-registry (local file, or the copy in the
private hermes-state repo when the host is fresh), downloads every part through the Bot
API, verifies sha256 of the joined archive and unpacks it:

  * `uploads/…`, `generated_images/…` → `/home/ai_service/webui_data/`
  * `sandbox_users/<slug>/…` → `/home/ai_service/sandbox/users/<slug>/` (members are
    re-rooted: ownership ubuntu:ubuntu like the sandbox containers run as, except
    `.agents`, which the provisioner requires to be root-owned).

Usage:
  webui-data-restore.py --dry-run               # show what would be pulled
  webui-data-restore.py                         # restore into an empty webui_data/
  webui-data-restore.py --force                 # overwrite existing files
  webui-data-restore.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "/home/ai_service/notifications")
import monitor  # noqa: E402

TG_API = "https://api.telegram.org"
WORK = Path("/var/lib/webui-data-backup/restore")
WEBUI_DATA = Path("/home/ai_service/webui_data")
SANDBOX_USERS = Path("/home/ai_service/sandbox/users")
SANDBOX_PREFIX = "sandbox_users"
SB_UID = SB_GID = 1000  # open-terminal sandbox runs as 1000:1000 (ubuntu)
DB_NAME = "webui_data"
AI_USER = "ai_service"


def log(message: str) -> None:
    print(message, flush=True)


def fetch_registry(url: str | None) -> dict:
    if url:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    path = monitor.tg_registry_path()
    if not path.is_file():
        raise SystemExit(f"registry not found: {path} (use --registry URL)")
    return json.loads(path.read_text())


def newest_set(registry: dict) -> dict:
    sets = [s for s in registry.get("sets", []) if isinstance(s, dict) and s.get("db") == DB_NAME]
    if not sets:
        raise SystemExit(f"no '{DB_NAME}' set in registry")
    return max(sets, key=lambda s: str(s.get("created_at", "")))


def http_get(url: str, timeout: int = 600) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download(token: str, entry: dict, work: Path) -> Path:
    name = str(entry.get("dump") or "")
    parts = sorted(entry.get("parts") or [], key=lambda p: str(p.get("name")))
    work.mkdir(parents=True, exist_ok=True)
    joined = work / name
    total_expected = int(entry.get("size") or 0)
    with joined.open("wb") as out:
        for part in parts:
            file_id = str(part.get("file_id") or "")
            if not file_id:
                raise SystemExit(f"{part.get('name')}: no file_id")
            meta = json.loads(http_get(f"{TG_API}/bot{token}/getFile?file_id={file_id}"))
            if not meta.get("ok"):
                raise SystemExit(f"getFile {part.get('name')}: {meta.get('description')}")
            remote = meta["result"]
            blob = http_get(f"{TG_API}/file/bot{token}/{remote['file_path']}")
            want = part.get("sha256")
            if want and hashlib.sha256(blob).hexdigest() != want:
                raise SystemExit(f"{part.get('name')}: sha256 mismatch")
            if int(part.get("size") or 0) and len(blob) != int(part["size"]):
                raise SystemExit(f"{part.get('name')}: size {len(blob)} != {part['size']}")
            out.write(blob)
            log(f"  ✓ {part.get('name')} ({len(blob) / 1024**2:.1f} MB)")

    size = joined.stat().st_size
    if total_expected and size != total_expected:
        raise SystemExit(f"{name}: joined {size} B != expected {total_expected} B")
    digest = hashlib.sha256()
    with joined.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    if entry.get("sha256") and digest.hexdigest() != entry["sha256"]:
        raise SystemExit(f"{name}: archive sha256 mismatch — download corrupted")
    log(f"archive ok: {name} ({size / 1024**2:.1f} MB)")
    return joined


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except OSError:
        pass
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
            except OSError:
                continue


def _strip_prefix(member: tarfile.TarInfo, dest: Path) -> tarfile.TarInfo:
    """tarfile-фильтр для членов sandbox_users/<slug>/…: срезать префикс перед data-фильтром."""
    if member.name.startswith(SANDBOX_PREFIX + "/"):
        member.name = member.name[len(SANDBOX_PREFIX) + 1:]
    return tarfile.data_filter(member, dest)


def _wipe(target: Path) -> None:
    if not target.is_dir():
        return
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _extract_members(tar: tarfile.TarFile, members: list, target: Path,
                     rename: bool = False) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if rename:
        extract_filter = _strip_prefix
    else:
        extract_filter = "data"
    try:
        tar.extractall(target, members=members, filter=extract_filter)
    except TypeError:  # Python < 3.12
        tar.extractall(target, members=members)


def _norm(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def _slug_of(name: str) -> str:
    parts = name.split("/")
    return parts[1] if len(parts) >= 2 else ""


def extract(archive: Path, target: Path, force: bool) -> None:
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        for m in members:
            m.name = _norm(m.name)
        sb_members = [
            m for m in members
            if m.name == SANDBOX_PREFIX or m.name.startswith(SANDBOX_PREFIX + "/")
        ]
        sb_ids = {id(m) for m in sb_members}
        data_members = [m for m in members if id(m) not in sb_ids]

        if target.is_dir() and any(target.iterdir()):
            if not force:
                raise SystemExit(f"{target} is not empty — use --force to overwrite")
            _wipe(target)
        if data_members:
            _extract_members(tar, data_members, target)
            try:
                shutil.chown(target, AI_USER, AI_USER)
                for path in target.rglob("*"):
                    shutil.chown(path, AI_USER, AI_USER)
            except (KeyError, PermissionError):
                pass
            log(f"extracted webui data into {target}")

        slugs = sorted({s for s in (_slug_of(m.name) for m in sb_members) if s})
        if not slugs:
            return
        SANDBOX_USERS.mkdir(parents=True, exist_ok=True)
        wanted: list[str] = []
        for slug in slugs:
            user_dir = SANDBOX_USERS / slug
            if user_dir.exists() and force:
                _wipe(user_dir)
            elif user_dir.exists() and any(user_dir.iterdir()):
                log(f"skip {slug}: {user_dir} exists and is not empty (use --force)")
                continue
            wanted.append(slug)
        if not wanted:
            return
        wanted_set = set(wanted)
        selected = [m for m in sb_members if _slug_of(m.name) in wanted_set]
        _extract_members(tar, selected, SANDBOX_USERS, rename=True)
        for slug in wanted:
            user_dir = SANDBOX_USERS / slug
            _chown_tree(user_dir, SB_UID, SB_GID)
            # provisioner требует root-owned .agents — возвращаем как было
            agents = user_dir / ".agents"
            if agents.is_dir():
                _chown_tree(agents, 0, 0)
            log(f"extracted sandbox user {slug} → {user_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", help="URL реестра (например, raw из hermes-state)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="перезаписать непустой webui_data")
    parser.add_argument("--target", default=str(WEBUI_DATA))
    args = parser.parse_args()

    token = os.environ.get("FILE_BOT_TOKEN")
    if not token:
        raise SystemExit("FILE_BOT_TOKEN missing (notifications/.env)")

    entry = newest_set(fetch_registry(args.registry))
    log(
        f"set: {entry['dump']} · {int(entry.get('size', 0)) / 1024**2:.0f} MB · "
        f"{len(entry.get('parts') or [])} parts · {entry.get('created_at')}"
    )
    if args.dry_run:
        log("[dry-run] nothing downloaded")
        return 0

    archive = download(token, entry, WORK)
    extract(archive, Path(args.target), args.force)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
