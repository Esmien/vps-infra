#!/usr/bin/env python3
"""Restore Open WebUI files (uploads + generated images) from the Telegram file bot.

Reads the newest `webui_data` set from the tg-registry (local file, or the copy in the
private hermes-state repo when the host is fresh), downloads every part through the Bot
API, verifies sha256 of the joined archive and unpacks it into webui_data/.

Usage:
  webui-data-restore.py --dry-run               # show what would be pulled
  webui-data-restore.py                         # restore into an empty webui_data/
  webui-data-restore.py --force                 # overwrite an existing webui_data/
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


def extract(archive: Path, target: Path, force: bool) -> None:
    if target.is_dir() and any(target.iterdir()):
        if not force:
            raise SystemExit(f"{target} is not empty — use --force to overwrite")
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(target, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(target)
    try:
        shutil.chown(target, AI_USER, AI_USER)
        for path in target.rglob("*"):
            shutil.chown(path, AI_USER, AI_USER)
    except (KeyError, PermissionError):
        pass
    log(f"extracted into {target}")


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
