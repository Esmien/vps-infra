#!/usr/bin/env python3
"""Публикует канонический bootstrap.sh в публичный репозиторий Esmien/vps-bootstrap.

Канон живёт в приватном vps-infra (там же, где install.py); публичная копия нужна
только чтобы `curl … | bash` работал без токена. Скрипт сравнивает содержимое и
делает коммит лишь при изменении, поэтому копии не расходятся.

  ./maintainer/publish_bootstrap.py            # обновить, если отличается
  ./maintainer/publish_bootstrap.py --check    # только проверить (exit 1 если разошлось)
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Esmien/vps-bootstrap"
BRANCH = "main"
API = "https://api.github.com"
SRC = Path(__file__).resolve().parent.parent / "bootstrap.sh"
ENV_FILE = Path("/home/ai_service/ai-stack/ai/.env")


def token() -> str:
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("GITHUB_KEY="):
            env["GITHUB_KEY"] = line.split("=", 1)[1].strip()
    key = env.get("GITHUB_KEY", "")
    if not key:
        sys.exit("нет GITHUB_KEY в ai/.env")
    return key


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"token {token()}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        sys.exit(f"GitHub API {method} {path}: HTTP {exc.code} {body}")


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    local = SRC.read_text()
    remote = call("GET", f"/repos/{REPO}/contents/bootstrap.sh?ref={BRANCH}")
    current = base64.b64decode(remote.get("content", "")).decode() if remote.get("content") else ""
    if current == local:
        print("bootstrap.sh в публичном репозитории совпадает с каноном")
        return 0
    if check_only:
        print("РАСХОЖДЕНИЕ: публичный bootstrap.sh отличается от канона")
        return 1
    call(
        "PUT",
        f"/repos/{REPO}/contents/bootstrap.sh",
        {
            "message": "sync: bootstrap.sh из vps-infra",
            "content": base64.b64encode(local.encode()).decode(),
            "branch": BRANCH,
            "sha": remote.get("sha"),
        },
    )
    print("bootstrap.sh опубликован")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
