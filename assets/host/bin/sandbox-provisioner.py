#!/usr/bin/env python3
"""Provisioner персональных песочниц Open WebUI.

Один прогон = одна сверка (таймер systemd раз в минуту, см. sandbox-provisioner.timer):

  - пользователь с ролью `user`  -> каталог, ключ, контейнер `ot-u-<slug>`, соединение в WebUI
    с грантом только этому пользователю (соединение появляется после healthy);
  - роль не `user` (pending/admin) -> грант и соединение убираются, контейнер стопается;
  - пользователь удалён -> соединение убрано, контейнер снят, каталог живёт RETENTION_DAYS;
  - каждый прогон: квота QUOTA на каталог, кроме пакетных каталогов (PROTECTED);
  - идемпотентно: повторный прогон ничего не ломает и дублей не создаёт.

Секреты (ключи песочниц) живут только в state.json (0600) и в окружении контейнера.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import stat
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

USERS_DIR = "/home/ai_service/sandbox/users"
TEMPLATE = "/home/ai_service/ai-stack/ai/sandbox-users/template.compose.yml"
STATE_PATH = "/home/ai_service/sandbox-users/state.json"
OWUI_ENV_PATH = "/root/.config/owui-signup-approver/env"
NOTIFY_ENV_PATH = "/home/ai_service/notifications/.env"
LOCK_PATH = "/run/sandbox-provisioner.lock"

QUOTA_BYTES = 2 * 1024**3
PROTECTED = {".local", "bin", "venv", ".arduino15", "Arduino", ".agents"}
RETENTION_DAYS = 3
CONTAINER_PREFIX = "ot-u-"
CONNECTION_NAME = "Песочница"  # префикс; к нему добавляется часть email до собаки
OWNER_UID_GID = 1000
DEFAULT_LIMITS = {"mem": "1536m", "memswap": "2g", "cpus": "1.0"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def parse_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                out[key] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def psql(query: str) -> list[list[str]]:
    res = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "webui_db", "-tA", "-F", "\t", "-c", query],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"psql: {res.stderr.strip()[:300]}")
    rows = []
    for line in res.stdout.strip().splitlines():
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def webui_users() -> dict[str, dict]:
    rows = psql('select id, coalesce(email,\'\'), role, coalesce(name,\'\') from "user"')
    return {
        r[0]: {"id": r[0], "email": r[1], "role": r[2], "name": r[3] if len(r) > 3 else ""}
        for r in rows
    }


def slug_for(user: dict, taken: set[str]) -> str:
    local = (user["email"] or "").split("@")[0].lower()
    base = re.sub(r"[^a-z0-9]+", "-", local).strip("-")[:24]
    if len(base) < 3:
        base = "u" + re.sub(r"[^a-z0-9]", "", user["id"].lower())[:8]
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


# ---------------------------------------------------------------- docker

def compose(entry: dict, action: list[str]) -> str:
    env = dict(os.environ)
    env.update(
        {
            "SB_SLUG": entry["slug"],
            "SB_KEY": entry["key"],
            "SB_DIR": entry["dir"],
            "SB_MEM": entry.get("mem", DEFAULT_LIMITS["mem"]),
            "SB_MEMSWAP": entry.get("memswap", DEFAULT_LIMITS["memswap"]),
            "SB_CPUS": str(entry.get("cpus", DEFAULT_LIMITS["cpus"])),
        }
    )
    cmd = ["docker", "compose", "-p", f"sb-{entry['slug']}", "-f", TEMPLATE, *action]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=USERS_DIR)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[-1]}: {res.stderr.strip()[:400]}")
    return res.stdout


def container_health(name: str) -> str:
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Health.Status}}|{{.State.Status}}", name],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def wait_healthy(name: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = container_health(name)
        if state.startswith("healthy"):
            return True
        time.sleep(3)
    return False


def ensure_dir(path: str) -> None:
    """Рабочий каталог + скрытый TMPDIR. Ничего лишнего в витрине: пользователь
    должен видеть в файловом браузере только свои файлы, без служебных папок."""
    # .tmp — служебный (скрыт), generated_images — видимая папка под картинки.
    for target, mode in ((path, 0o700), (os.path.join(path, ".tmp"), 0o700), (os.path.join(path, "generated_images"), 0o700)):
        os.makedirs(target, exist_ok=True)
        os.chown(target, OWNER_UID_GID, OWNER_UID_GID)
        os.chmod(target, mode)


AGENTS_BLOCK_BEGIN = "<!-- managed:begin -->"
AGENTS_BLOCK_END = "<!-- managed:end -->"
AGENTS_BODY = """# AGENTS

Системные правила и инструкции администратора модели имеют высший приоритет.
MEMORY.md содержит важный контекст бесед и проектов; PROFILE.md — сведения о пользователе.
Сохраняйте и удаляйте воспоминания только через инструмент памяти, если он доступен.
Перед переносом сведений между файлами спросите пользователя. Не удаляйте данные ради уборки.
При противоречии сведений MEMORY имеет преимущество над PROFILE; спорные записи не удаляйте наугад.
Новые явные сообщения пользователя имеют приоритет над устаревшими фактами.
Показывая записи памяти в чате, выводите их дословно, байт в байт, по одной записи в строке и с нумерацией: 1. <запись>, 2. <запись>. Не пересказывайте, не сокращайте, не объединяйте, не переставляйте и не дополняйте записи. Если точный список уже вывел фильтр памяти, не дублируйте его: используйте только его тексты и номера.
"""
AGENTS_TEMPLATE = AGENTS_BLOCK_BEGIN + "\n" + AGENTS_BODY + AGENTS_BLOCK_END + "\n"


LEGACY_AGENTS_TEXT = """# AGENTS

Системные правила и инструкции администратора модели имеют высший приоритет.
MEMORY.md содержит важный контекст бесед и проектов; PROFILE.md — сведения о пользователе.
Сохраняйте и удаляйте воспоминания только через инструмент памяти, если он доступен.
Перед переносом сведений между файлами спросите пользователя. Не удаляйте данные ради уборки.
Новые сообщения пользователя имеют приоритет над устаревшими фактами."""


def _norm_lines(text: str) -> str:
    return " ".join(text.split())


def _managed_only(text: str) -> bool:
    """Текст состоит только из строк управляемых шаблонов (старых или текущего)."""
    known = {_norm_lines(line) for line in (LEGACY_AGENTS_TEXT + "\n" + AGENTS_BODY).splitlines() if line.strip()}
    return all(_norm_lines(line) in known for line in text.splitlines() if line.strip())


def merge_agents_block(existing: str) -> str:
    """Заменить управляемый блок, сохранив собственный текст пользователя."""
    begin = existing.find(AGENTS_BLOCK_BEGIN)
    end = existing.find(AGENTS_BLOCK_END)
    if begin != -1 and end > begin:
        prefix = existing[:begin]
        tail = existing[end + len(AGENTS_BLOCK_END):]
        # Старый управляемый текст, оставшийся перед блоком, не должен жить рядом с новым.
        if not prefix.strip() or _managed_only(prefix):
            prefix = ""
        return prefix + AGENTS_TEMPLATE.rstrip("\n") + tail
    if 'Системные правила и инструкции администратора модели имеют высший приоритет.' in existing:
        # Прежний управляемый текст без маркеров: целиком заменяется новым блоком.
        return AGENTS_TEMPLATE
    body = existing.rstrip("\n")
    return (body + "\n\n" if body else "") + AGENTS_TEMPLATE


def user_system_prompt(uid: str) -> str:
    # UUID валидируется до SQL-интерполяции; содержимое промпта не попадает в лог.
    canonical = str(uuid.UUID(uid))
    if canonical != uid:
        raise ValueError("invalid user id")
    query = (
        "select encode(convert_to(coalesce(settings::jsonb->'ui'->>'system',''),"
        "'UTF8'),'base64') from \"user\" where id='" + canonical + "' and role='user'"
    )
    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "webui_db", "-tA", "-c", query],
        capture_output=True, text=True, check=True,
    )
    return base64.b64decode(''.join(result.stdout.split()), validate=True).decode('utf-8')


def ensure_memory_files(path: str, uid: str) -> None:
    """Создать отсутствующие файлы, не перезаписывая существующие и не следуя symlink."""
    root = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir('.agents', 0o755, dir_fd=root)
        except FileExistsError:
            pass
        directory = os.open('.agents', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            if os.fstat(directory).st_uid != 0:
                raise ValueError('memory directory must be root-owned')
            for name in ('AGENTS.md', 'MEMORY.md', 'PROFILE.md'):
                try:
                    existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    current = None
                    try:
                        if not stat.S_ISREG(os.fstat(existing).st_mode):
                            raise ValueError('memory file is not regular')
                        if name == 'AGENTS.md':
                            with os.fdopen(existing, 'r', encoding='utf-8', closefd=False) as stream:
                                current = stream.read()
                    finally:
                        os.close(existing)
                    if name == 'AGENTS.md':
                        merged = merge_agents_block(current or '')
                        if merged != current:
                            tmp = '.init-' + uuid.uuid4().hex
                            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
                            try:
                                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                                    stream.write(merged)
                                    stream.flush()
                                    os.fsync(stream.fileno())
                                os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
                                os.fsync(directory)
                            finally:
                                try:
                                    os.unlink(tmp, dir_fd=directory)
                                except FileNotFoundError:
                                    pass
                    continue
                if name == 'AGENTS.md':
                    text = AGENTS_TEMPLATE
                elif name == 'MEMORY.md':
                    text = '# MEMORY\n\n'
                else:
                    prompt = user_system_prompt(uid)
                    text = '# PROFILE\n\n' + prompt
                tmp = '.init-' + uuid.uuid4().hex
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                        stream.write(text)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(tmp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    os.fsync(directory)
                finally:
                    os.unlink(tmp, dir_fd=directory)
        finally:
            os.close(directory)
    finally:
        os.close(root)


# ---------------------------------------------------------------- WebUI API

def api(method: str, path: str, payload=None):
    env = parse_env(OWUI_ENV_PATH)
    key = env.get("OWUI_ADMIN_API_KEY", "")
    base = env.get("OWUI_BASE", "http://127.0.0.1:8080").rstrip("/")
    if not key:
        raise RuntimeError("OWUI_ADMIN_API_KEY отсутствует в " + OWUI_ENV_PATH)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode() or "{}"
        return json.loads(body)


def get_connections() -> list[dict]:
    res = api("GET", "/api/v1/configs/terminal_servers")
    if isinstance(res, list):
        return res
    return res.get("TERMINAL_SERVER_CONNECTIONS", [])


def set_connections(conns: list[dict]) -> None:
    api("POST", "/api/v1/configs/terminal_servers", {"TERMINAL_SERVER_CONNECTIONS": conns})


def desired_connection(entry: dict) -> dict:
    label = (entry.get("email") or entry["slug"]).split("@")[0]
    return {
        "id": entry["conn_id"],
        "name": f"{CONNECTION_NAME} · {label}",
        "enabled": True,
        "url": f"http://{entry['container']}:8000",
        "path": "/openapi.json",
        "key": entry["key"],
        "auth_type": "bearer",
        "forward_cookies": False,
        "config": {
            "access_grants": [
                {"principal_type": "user", "principal_id": entry["user_id"], "permission": "read"}
            ],
            "working_directory_context": True,
            "user_shell_tools": "auto",
            "chat_uploads": "filesystem",
            "sandbox_user": entry["user_id"],
        },
        "server_type": "terminal",
        "policy_id": None,
    }


def sync_connections(state: dict, dry_run: bool) -> bool:
    """Оставляет чужие соединения как есть, свои — приводит к состоянию."""
    current = get_connections()
    foreign = [c for c in current if not (c.get("config") or {}).get("sandbox_user")]
    ours = [desired_connection(e) for e in state.values() if e.get("active")]
    wanted = foreign + ours

    def norm(items):
        return json.dumps(sorted(items, key=lambda c: c.get("id", "")), sort_keys=True)

    if norm(current) == norm(wanted):
        return False
    if dry_run:
        log(f"[dry-run] соединения: было {len(current)}, станет {len(wanted)}")
        return True
    set_connections(wanted)
    log(f"соединения обновлены: {len(wanted)} (своих {len(ours)}, чужих {len(foreign)})")
    return True


# ---------------------------------------------------------------- квота

def dir_size(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        # Исключения — только каталоги верхнего уровня рабочего каталога
                        # (cur == path). Раньше условие было перевёрнуто и LRU лез внутрь
                        # ~/.local и ~/.arduino15, удаляя пакеты и тулчейны.
                        if cur == path and entry.name in PROTECTED:
                            continue
                        stack.append(entry.path)
                    else:
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def protected_size(path: str) -> int:
    total = 0
    for name in PROTECTED:
        sub = os.path.join(path, name)
        if os.path.isdir(sub):
            for root, _dirs, files in os.walk(sub):
                for f in files:
                    try:
                        total += os.stat(os.path.join(root, f), follow_symlinks=False).st_size
                    except OSError:
                        pass
    return total


def enforce_quota(entry: dict, dry_run: bool) -> None:
    path = entry["dir"]
    size = dir_size(path)
    prot = protected_size(path)
    # Порог алерта по пакетным каталогам можно поднять отдельному пользователю
    # (например тому, кому сознательно ставят тулчейн Arduino на 7 ГБ).
    prot_limit = int(entry.get("protected_alert_bytes", QUOTA_BYTES))
    if size <= QUOTA_BYTES:
        if prot > prot_limit:
            log(f"WARN {entry['slug']}: пакетные каталоги {prot/1024**3:.2f} ГБ > порога {prot_limit/1024**3:.1f} ГБ при рабочих {size/1024**3:.2f} ГБ")
            notify(f"Песочница {entry['slug']}: пакеты занимают {prot/1024**3:.1f} ГБ (порог {prot_limit/1024**3:.1f} ГБ). Решение за тобой.")
        return

    files: list[tuple[float, str, int]] = []
    for root, dirs, names in os.walk(path):
        if root == path:
            dirs[:] = [d for d in dirs if d not in PROTECTED]
        for name in names:
            full = os.path.join(root, name)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            files.append((st.st_mtime, full, st.st_size))
    files.sort()
    removed, freed = 0, 0
    for _mtime, full, size_f in files:
        if size - freed <= QUOTA_BYTES:
            break
        if dry_run:
            log(f"[dry-run] удалил бы {full} ({size_f/1024**2:.1f} МБ)")
        else:
            try:
                os.unlink(full)
                removed += 1
                freed += size_f
            except OSError as exc:
                log(f"не смог удалить {full}: {exc}")
    if not dry_run:
        log(f"квота {entry['slug']}: удалено {removed} файл(ов), освобождено {freed/1024**2:.1f} МБ")
        notify(f"Песочница {entry['slug']}: превышен лимит 2 ГБ, удалено {removed} старых файлов ({freed/1024**2:.0f} МБ).")


def notify(text: str) -> None:
    env = parse_env(NOTIFY_ENV_PATH)
    token, chat = env.get("BOT_TOKEN"), env.get("BOT_OWNER")
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=15
        )
    except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять сверку
        log(f"notify failed: {exc}")


# ---------------------------------------------------------------- состояние

def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- сверка

def reconcile(dry_run: bool, only: str | None) -> int:
    users = webui_users()
    state = load_state()
    taken = {e["slug"] for e in state.values()}
    actions = 0

    # 1. Новые пользователи -> контейнер + соединение
    active_users = {uid: u for uid, u in users.items() if u["role"] in ("user", "admin")}
    for uid, user in active_users.items():
        if only and only.lower() not in (user["email"] or "").lower():
            continue
        entry = state.get(uid)
        if entry is None:
            slug = slug_for(user, taken)
            taken.add(slug)
            entry = {
                "user_id": uid,
                "slug": slug,
                "email": user["email"],
                "container": f"{CONTAINER_PREFIX}{slug}",
                "dir": os.path.join(USERS_DIR, slug),
                "key": secrets.token_urlsafe(32),
                "conn_id": str(uuid.uuid4()),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active": True,
                **DEFAULT_LIMITS,
            }
            if dry_run:
                log(f"[dry-run] создал бы песочницу {slug} для {user['email']}")
                actions += 1
                continue
            ensure_dir(entry["dir"])
            ensure_memory_files(entry["dir"], uid)
            compose(entry, ["up", "-d"])
            if not wait_healthy(entry["container"]):
                log(f"ОШИБКА {slug}: контейнер не стал healthy, соединение не создаю")
                entry["active"] = False
                state[uid] = entry
                continue
            state[uid] = entry
            log(f"создана песочница {slug} ({entry['container']}) для {user['email']}")
            actions += 1
            continue

        # существующий: `up -d` идемпотентен, но пересоздаёт контейнер, если шаблон
        # изменился (пути, лимиты, промпт) — иначе правки шаблона не доезжают до людей.
        if not entry.get("active"):
            continue
        if not dry_run:
            ensure_dir(entry["dir"])  # папки могли появиться позже (например generated_images)
            ensure_memory_files(entry["dir"], uid)
        health = container_health(entry["container"])
        if dry_run:
            if not health:
                log(f"[dry-run] поднял бы {entry['container']}")
                actions += 1
            continue
        before = health
        compose(entry, ["up", "-d"])
        if before != container_health(entry["container"]) or not before:
            if not wait_healthy(entry["container"]):
                log(f"ОШИБКА {entry['container']}: не healthy после подъёма")
                continue
            log(f"синхронизирован {entry['container']} (было: {before or 'нет'})")
            actions += 1

    # 2. Потерявшие право (роль не user) и удалённые пользователи
    for uid, entry in list(state.items()):
        user = users.get(uid)
        if user and user["role"] in ("user", "admin"):
            continue
        if entry.get("active"):
            if dry_run:
                log(f"[dry-run] снял бы грант и остановил {entry['container']} ({entry['email']})")
                entry["active"] = False
                actions += 1
                continue
            compose(entry, ["down"])
            entry["active"] = False
            entry["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log(f"остановлена песочница {entry['slug']} (роль {user['role'] if user else 'удалён'})")
            actions += 1
        else:
            stopped = entry.get("stopped_at")
            if not stopped:
                continue
            if time.time() - time.mktime(time.strptime(stopped, "%Y-%m-%dT%H:%M:%SZ")) < RETENTION_DAYS * 86400:
                continue
            if dry_run:
                log(f"[dry-run] удалил бы каталог {entry['dir']}")
                continue
            shutil.rmtree(entry["dir"], ignore_errors=True)
            log(f"удалён каталог истёкшего пользователя: {entry['dir']}")
            state.pop(uid, None)
            actions += 1

    # 3. Соединения
    if sync_connections(state, dry_run):
        actions += 1

    # 4. Квоты
    for entry in state.values():
        if entry.get("active"):
            enforce_quota(entry, dry_run)

    if not dry_run:
        save_state(state)
    return actions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="только один пользователь (по части email)")
    args = ap.parse_args()

    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("предыдущий прогон ещё идёт, выходим")
        return 0

    try:
        changed = reconcile(args.dry_run, args.only)
        log(("dry-run: " if args.dry_run else "") + f"изменений: {changed}")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"ОШИБКА: {type(exc).__name__}: {exc}")
        return 1
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
