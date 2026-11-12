#!/usr/bin/env python3
"""Выгружает аватарки моделей Open WebUI в статический кэш для nginx.

Зачем: SPA на холодном старте запрашивает иконки моделей до восстановления
сессии, получает 401 и навсегда подменяет картинку дефолтным логотипом
(повторной попытки в клиенте нет). Nginx отдаёт эти же файлы напрямую из
кэша (`try_files` в location /api/v1/models/model/profile/image), поэтому
гонка авторизации на иконки больше не влияет.

Источник истины — webui_db.model.meta.profile_image_url (data:image/...).
Кэш вторичен: файл только дописывается/обновляется, ничего не удаляется.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys

CACHE_DIR = "/var/cache/owui-avatars"
DB = "webui_db"
EXT = {
    "image/webp": "webp",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$")
SQL = (
    "select coalesce(json_agg(json_build_object("
    "'id', id, 'img', (meta::jsonb)->>'profile_image_url')), '[]') "
    "from model "
    "where coalesce((meta::jsonb)->>'profile_image_url','') like 'data:image/%';"
)


def rows() -> list[dict]:
    out = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", DB, "-Atc", SQL],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def main() -> int:
    os.makedirs(CACHE_DIR, exist_ok=True)
    written = unchanged = skipped = 0
    keep: set[str] = set()
    for row in rows():
        mid, url = row["id"], row["img"] or ""
        if not ID_RE.match(mid) or ".." in mid:
            print(f"skip id (unsafe): {mid!r}", file=sys.stderr)
            skipped += 1
            continue
        header, _, payload = url.partition(",")
        mime = header.split(";")[0].removeprefix("data:").lower()
        ext = EXT.get(mime)
        if not ext:
            print(f"skip id (mime {mime}): {mid}", file=sys.stderr)
            skipped += 1
            continue
        try:
            blob = base64.b64decode(payload, validate=False)
        except Exception as exc:  # noqa: BLE001
            print(f"skip id (base64): {mid}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        path = os.path.join(CACHE_DIR, f"{mid}.{ext}")
        keep.add(os.path.basename(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) == len(blob):
            with open(path, "rb") as fh:
                if fh.read() == blob:
                    unchanged += 1
                    continue
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.chmod(tmp, 0o644)
        os.chown(tmp, 0, 33)  # root:www-data
        os.replace(tmp, path)
        written += 1

    orphans = 0
    for dirpath, _dirs, files in os.walk(CACHE_DIR):
        for name in files:
            if name.endswith(".tmp") or name not in keep:
                orphans += 1

    print(f"avatars: written={written} unchanged={unchanged} skipped={skipped} orphaned={orphans} dir={CACHE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
