#!/usr/bin/env python3
"""Offsite copy of the config secrets (.env files), encrypted, to the Telegram file bot.

Why: `.env` files are git-ignored on purpose, so after a total host loss the recovery
repos restore *code and config* but not credentials — and fetching new bot tokens,
LiteLLM keys and DB passwords by hand is the slowest step of a rebuild.

Layout:
  /var/lib/secrets-offsite/secrets_<stamp>.tar.gz.enc   AES-256-CBC, pbkdf2
  /var/lib/secrets-offsite/secrets_<stamp>.inventory.txt  key names only, no values
  /root/.secrets/offsite.key                            the passphrase (600 root)

The passphrase is generated once and delivered to the owner's private notification
chat exactly once (first run, flag `/root/.secrets/offsite.key.sent`). The encrypted
archive goes to the *file* bot — two different chats, so one compromised chat is not
enough. Nothing secret is ever printed by this script.

Every archive is round-trip verified (decrypt → sha256 → tar listing) before it is
uploaded; a failed check aborts the run and sends no file.

Usage:
    secrets-offsite-backup.py [--dry-run]
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ai_service/notifications")
import monitor  # noqa: E402  (reuses send_document/send_telegram/registry, loads .env on import)

SOURCE_PATTERNS = (
    "/home/ai_service/ai-stack/ai/.env",
    "/home/ai_service/notifications/.env",
    "/root/.hermes/.env",
    "/root/deploy/music_bot/**/.env",
    "/home/ai_service/ai-stack/ai/*/.env",
)
WORK = Path("/var/lib/secrets-offsite")
KEY_DIR = Path("/root/.secrets")
KEY_FILE = KEY_DIR / "offsite.key"
KEY_SENT_FLAG = KEY_DIR / "offsite.key.sent"
KEEP_ARCHIVES = 2
DB_NAME = "secrets_offsite"
PREFIX = "secrets"
PBKDF2_ITER = 300000


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def collect_sources() -> list[Path]:
    found: list[Path] = []
    for pattern in SOURCE_PATTERNS:
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            if path.is_file() and path not in found:
                found.append(path)
    return found


def key_names(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.append(line.split("=", 1)[0])
    return sorted(set(names))


def ensure_key() -> str:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    if not KEY_FILE.exists():
        KEY_FILE.write_text(subprocess.check_output(["openssl", "rand", "-hex", "32"]).decode().strip() + "\n")
        os.chmod(KEY_FILE, 0o600)
        log("generated new passphrase")
    return KEY_FILE.read_text().strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_archive(sources: list[Path]) -> tuple[Path, Path, Path]:
    WORK.mkdir(parents=True, exist_ok=True)
    os.chmod(WORK, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tar_path = WORK / f"{PREFIX}_{stamp}.tar.gz"
    inventory_path = WORK / f"{PREFIX}_{stamp}.inventory.txt"
    with tarfile.open(tar_path, "w:gz", compresslevel=9) as tar:
        for path in sources:
            tar.add(path, arcname=str(path).lstrip("/"))
    os.chmod(tar_path, 0o600)

    lines = [f"# secrets inventory {stamp} — key names only, no values", ""]
    for path in sources:
        lines.append(f"{path}")
        lines.append(f"  sha256: {sha256(path)}")
        for name in key_names(path):
            lines.append(f"  - {name}")
        lines.append("")
    inventory_path.write_text("\n".join(lines))
    os.chmod(inventory_path, 0o600)
    log(f"archived {len(sources)} .env files → {tar_path.name} ({monitor.format_size(tar_path.stat().st_size)})")
    return tar_path, inventory_path, WORK / f"{PREFIX}_{stamp}.tar.gz.enc"


def encrypt(tar_path: Path, enc_path: Path) -> str:
    key = ensure_key()
    subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(PBKDF2_ITER),
         "-salt", "-in", str(tar_path), "-out", str(enc_path), "-pass", f"file:{KEY_FILE}"],
        check=True,
    )
    os.chmod(enc_path, 0o600)
    log(f"encrypted → {enc_path.name} ({monitor.format_size(enc_path.stat().st_size)})")
    return key


def verify(enc_path: Path, tar_path: Path) -> None:
    """Decrypt with the same passphrase and compare content hashes; abort on mismatch."""
    with tempfile.TemporaryDirectory(prefix="secrets_verify_") as tmp:
        check = Path(tmp) / "check.tar.gz"
        subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", str(PBKDF2_ITER),
             "-in", str(enc_path), "-out", str(check), "-pass", f"file:{KEY_FILE}"],
            check=True,
        )
        if sha256(check) != sha256(tar_path):
            raise SystemExit("verify: decrypt mismatch — nothing sent")
        with tarfile.open(check, "r:gz") as tar:
            members = [m.name for m in tar.getmembers() if m.isfile()]
    if not members:
        raise SystemExit("verify: archive has no files — nothing sent")
    log(f"verify ok: {len(members)} files round-trip")


def prune() -> None:
    for pattern in (f"{PREFIX}_*.tar.gz", f"{PREFIX}_*.tar.gz.enc", f"{PREFIX}_*.inventory.txt"):
        files = sorted(WORK.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_ARCHIVES:]:
            old.unlink(missing_ok=True)
            log(f"pruned local {old.name}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    sources = collect_sources()
    if not sources:
        log("no .env files found — nothing to do")
        return 1

    tar_path, inventory_path, enc_path = build_archive(sources)
    key = encrypt(tar_path, enc_path)
    verify(enc_path, tar_path)

    if dry_run:
        log(f"dry-run: not sending {enc_path.name}")
        tar_path.unlink(missing_ok=True)
        return 0

    token = os.environ.get("FILE_BOT_TOKEN")
    chat_id = os.environ.get("BOT_OWNER")
    if not token or not chat_id:
        log("FILE_BOT_TOKEN/BOT_OWNER missing in notifications/.env")
        return 1

    sent = [(enc_path, monitor.send_document(token, chat_id, enc_path))]
    monitor.send_document(token, chat_id, inventory_path)
    monitor.record_backup_registry(
        monitor.backup_registry_entry(DB_NAME, enc_path, chat_id, sent, True)
    )
    # Шифрованный архив ушёл, inventory тоже — открытый tar с секретами на диске не нужен.
    tar_path.unlink(missing_ok=True)
    monitor.send_telegram(
        "🔐 <b>Секреты .env — офсайт-копия</b>\n"
        f"{enc_path.name} · {len(sources)} файлов · AES-256\n"
        f"Расшифровка: <code>openssl enc -d -aes-256-cbc -pbkdf2 -iter {PBKDF2_ITER} "
        f"-in {enc_path.name} -out secrets.tar.gz -pass pass:&lt;ключ&gt;</code>",
        parse_mode="HTML",
        token=token,
        chat_id=chat_id,
    )

    if not KEY_SENT_FLAG.exists():
        monitor.send_telegram(
            "🔑 <b>Ключ офсайт-копии секретов</b> (сохрани в менеджер паролей)\n"
            f"<code>{key}</code>\n"
            "Это единственная копия, которая не лежит на сервере. Без неё расшифровать "
            "архив из файл-бота нельзя.",
            parse_mode="HTML",
        )
        KEY_SENT_FLAG.write_text(datetime.now(timezone.utc).isoformat() + "\n")
        os.chmod(KEY_SENT_FLAG, 0o600)
        log("passphrase delivered once to the notification chat")

    prune()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
