#!/usr/bin/env python3
"""
vps-infra — установщик всей инфраструктуры VPS.

Что разворачивает:
  * PostgreSQL 17 + pgvector на хосте, роли и базы webui/litellm
  * Docker + compose-стек: LiteLLM, Open WebUI, SearXNG, open-terminal, Redis
  * Nginx + Certbot (TLS)
  * Telegram-мониторинг и бэкапы (systemd-юниты и таймеры)
  * восстановление БД и данных Open WebUI из bundle (опционально)
  * в финале — установку Hermes в хостовом режиме (по желанию)

Идемпотентность: каждый шаг сначала проверяет фактическое состояние.
Повторный запуск не трогает то, что уже настроено, и не перезаписывает
существующие секреты.

Использование:
  install.py                      интерактивная установка
  install.py --yes                без вопросов (существующие значения/дефолты)
  install.py --reconfigure        переспросить секреты (Enter = оставить)
  install.py check                только проверка, ничего не меняет
  install.py export [--with-data] собрать переносимый bundle
  install.py --help

Полезные флаги:
  --skip restore,tls      пропустить шаги
  --only stack,verify     выполнить только выбранные шаги
  --bundle PATH|URL       архив с дампами БД (и данными WebUI)
  --restore               восстановить БД даже поверх существующих данных
  --skip-tls              не трогать nginx/certbot
  --hermes / --no-hermes  установить/не устанавливать Hermes
  --dry-run               показать, что будет сделано
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
TEMPLATES = ASSETS / "templates"

DEFAULT_INFRA_DIR = Path(os.environ.get("VPS_INFRA_DEFAULT_DIR", "/home/ai_service/ai-stack"))
STATE_DIR = Path(os.environ.get("VPS_INFRA_STATE", "/var/lib/vps-infra"))
CONFIG_FILE = Path(os.environ.get("VPS_INFRA_CONFIG", "/etc/vps-infra/install.json"))
LOG_FILE = Path(os.environ.get("VPS_INFRA_LOG", "/var/log/vps-infra-install.log"))

AI_USER = "ai_service"
AI_HOME = Path("/home/ai_service")
WEBUI_DATA = AI_HOME / "webui_data"
NOTIF_DIR = AI_HOME / "notifications"

PG_VERSION = "17"
DOCKER_NET = "stack-net"
STALE_HOURS = 72
HERMES_INSTALL_URL = "https://hermes-agent.nousresearch.com/install.sh"

# ── цвета / вывод ────────────────────────────────────────────────────────────

C_RESET = "\033[0m"
C_RED = "\033[1;31m"
C_GREEN = "\033[1;32m"
C_YELLOW = "\033[1;33m"
C_CYAN = "\033[1;36m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"


def step(title: str) -> None:
    print(f"\n{C_CYAN}{C_BOLD}==> {title}{C_RESET}")


def ok(msg: str) -> None:
    print(f"  {C_GREEN}✓{C_RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {C_DIM}·{C_RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {C_YELLOW}!{C_RESET} {msg}", file=sys.stderr)


class InstallError(RuntimeError):
    pass


def die(msg: str) -> None:
    raise InstallError(msg)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}…{value[-3:]}"


def sql_lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def parse_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


# ── шаги ─────────────────────────────────────────────────────────────────────

STEPS: list[tuple[str, str]] = [
    ("preflight", "Проверка окружения и сбор параметров"),
    ("packages", "Системные пакеты и Docker"),
    ("users", "Пользователь ai_service и каталоги"),
    ("stack", "Файлы стека"),
    ("secrets", "Секреты и .env"),
    ("bundle", "Bundle с дампами"),
    ("postgres", "PostgreSQL 17 + pgvector"),
    ("network", "Docker-сеть"),
    ("images", "Docker-образы"),
    ("migrate", "Роли, базы и миграции LiteLLM"),
    ("restore", "Восстановление БД"),
    ("apps", "Запуск стека"),
    ("notifications", "Telegram-мониторинг"),
    ("units", "systemd-юниты и таймеры"),
    ("tls", "Nginx + сертификаты"),
    ("verify", "Проверка результата"),
    ("hermes", "Hermes"),
]
STEP_NAMES = [s[0] for s in STEPS]

# Юниты, которые ставит установщик (из assets/host/systemd).
CORE_TIMERS = [
    "docker-autorecover.timer",
    "docker-detector-liveness.timer",
    "litellm-spend-retention.timer",
    "litellm-watchdog.timer",
    "open-terminal-restart.timer",
    "postgres-backup.timer",
    "postgres-backup-notify.timer",
    "searxng-quality.timer",
    "webui-compact-images.timer",
    "webui-generated-images-retention.timer",
]
CORE_SERVICES = ["fw-guard-postgres.service"]
NOTIFY_SERVICES = [
    "server-monitor-bot.service",
    "server-monitor-events.service",
    "owui-signup-approver.service",
]
HERMES_UNITS = [
    "hermes-boot-notify.service",
    "hermes-state-watch.service",
    "hermes-state-backup.timer",
]

HOST_BIN_SCRIPTS = [
    "docker-detector-liveness.sh",
    "docker-incident-agent.sh",
    "docker-incident-detector.py",
    "fw-guard-postgres.sh",
    "litellm-spend-retention.sh",
    "litellm-watchdog.py",
    "open-terminal-restart.sh",
    "owui-signup-approver.py",
    "searxng-quality-check.py",
    "webui-compact-images.sh",
    "webui-generated-images-retention.sh",
    "hermes-boot-notify.sh",
    "hermes-state-backup.sh",
    "hermes-state-watch.sh",
]

BACKEND_PORTS = {"8080": "ai", "4000": "llm", "8000": "terminal"}


class Installer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.dry = args.dry_run
        self.check_mode = bool(getattr(args, "check_mode", False))
        self.log_handle = None
        self.cfg: dict = {}
        self.env: dict[str, str] = {}
        self.failed: list[str] = []
        self.changed: list[str] = []
        self.infra_dir = Path(args.infra_dir or DEFAULT_INFRA_DIR)
        self.compose_dir = self.infra_dir / "ai"
        self.compose_file = self.compose_dir / "docker-compose.yml"
        self.dump_dir = self.infra_dir / "backups" / "postgres"

    # ── инфраструктура логирования ──────────────────────────────────────────

    def open_log(self) -> None:
        if self.dry or self.check_mode:
            return
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.log_handle = LOG_FILE.open("a")
            self.log_handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} install.py {' '.join(sys.argv[1:])}\n")
            self.log_handle.flush()
        except OSError:
            self.log_handle = None

    def log(self, msg: str) -> None:
        if self.log_handle:
            try:
                self.log_handle.write(msg + "\n")
                self.log_handle.flush()
            except OSError:
                pass

    # ── запуск команд ───────────────────────────────────────────────────────

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        quiet: bool = True,
        capture: bool = True,
        input_text: str | None = None,
        env: dict | None = None,
        cwd: Path | None = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> subprocess.CompletedProcess:
        printable = " ".join(shlex.quote(str(c)) for c in cmd)
        self.log("$ " + printable)
        if self.dry:
            info(f"[dry-run] {printable}")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        full_env = dict(os.environ)
        full_env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        if env:
            full_env.update(env)
        try:
            if stream:
                proc = subprocess.run(cmd, cwd=cwd, env=full_env, timeout=timeout)
                res = subprocess.CompletedProcess(cmd, proc.returncode, "", "")
            else:
                res = subprocess.run(
                    cmd,
                    cwd=cwd,
                    env=full_env,
                    input=input_text,
                    text=True,
                    capture_output=capture,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            die(f"команда не завершилась за {timeout}s: {printable}")
        except FileNotFoundError:
            die(f"команда не найдена: {cmd[0]}")
        if check and res.returncode != 0:
            self.log(f"! exit {res.returncode}")
            out = (res.stdout or "") + (res.stderr or "")
            tail = "\n".join(out.strip().splitlines()[-15:])
            if tail:
                self.log(tail)
                print(tail, file=sys.stderr)
            die(f"команда завершилась с кодом {res.returncode}: {printable}")
        if not quiet and res.stdout:
            print(res.stdout, end="")
        return res

    def shell(self, script: str, **kw) -> subprocess.CompletedProcess:
        return self.run(["bash", "-c", script], **kw)

    def apt_install(self, packages: list[str]) -> None:
        if not packages:
            return
        self.run(["apt-get", "update", "-qq"], check=True)
        self.run(
            ["apt-get", "install", "-y", "-qq", "--no-install-recommends", *packages],
            timeout=1800,
        )

    def psql(self, sql: str, db: str = "postgres", check: bool = True) -> subprocess.CompletedProcess:
        return self.run(
            ["sudo", "-u", "postgres", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db],
            input_text=sql,
            check=check,
        )

    def psql_scalar(self, sql: str, db: str = "postgres") -> str:
        res = self.run(["sudo", "-u", "postgres", "psql", "-X", "-tA", "-d", db, "-c", sql])
        return (res.stdout or "").strip()

    def compose(self, *args: str, **kw) -> subprocess.CompletedProcess:
        cmd = [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "--project-directory",
            str(self.compose_dir),
            *args,
        ]
        return self.run(cmd, **kw)

    # ── файлы ───────────────────────────────────────────────────────────────

    def ensure_dir(self, path: Path, mode: int = 0o755, owner: str | None = None) -> None:
        if self.check_mode:
            return
        if not path.is_dir():
            if self.dry:
                info(f"[dry-run] mkdir -p {path}")
                return
            path.mkdir(parents=True, exist_ok=True)
            self.changed.append(f"dir {path}")
        if not self.dry:
            os.chmod(path, mode)
            if owner:
                shutil.chown(path, owner, owner)

    def write_file(self, path: Path, content: str, mode: int = 0o644, owner: str | None = None) -> bool:
        """Атомарно пишет файл, если содержимое изменилось. True = записан."""
        if self.check_mode:
            return False
        if path.is_file():
            try:
                if path.read_text() == content:
                    if owner and not self.dry:
                        try:
                            st = path.stat()
                            uid = __import__("pwd").getpwnam(owner).pw_uid
                            if st.st_uid != uid:
                                shutil.chown(path, owner, owner)
                        except KeyError:
                            pass
                    return False
            except OSError:
                pass
        if self.dry:
            info(f"[dry-run] записал бы {path}")
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            os.chmod(tmp, mode)
            if owner:
                shutil.chown(tmp, owner, owner)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.changed.append(str(path))
        return True

    def copy_file(self, src: Path, dst: Path, mode: int = 0o644, owner: str | None = None) -> bool:
        data = src.read_text(errors="replace")
        return self.write_file(dst, data, mode=mode, owner=owner)

    def sync_tree(self, src: Path, dst: Path, owner: str, skip: tuple[str, ...] = ()) -> int:
        n = 0
        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            if any(part in skip for part in rel.parts):
                continue
            target = dst / rel
            if item.is_dir():
                self.ensure_dir(target, 0o755, owner)
                continue
            if self.copy_file(item, target, 0o644, owner):
                n += 1
        for d in [dst, *[p for p in dst.rglob("*") if p.is_dir()]]:
            if not self.dry:
                try:
                    shutil.chown(d, owner, owner)
                except (KeyError, PermissionError):
                    pass
        return n

    # ── конфигурация и секреты ──────────────────────────────────────────────

    def load_config(self) -> None:
        self.cfg = {}
        if CONFIG_FILE.is_file():
            try:
                self.cfg = json.loads(CONFIG_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                self.cfg = {}
        if self.args.infra_dir:
            self.cfg["infra_dir"] = str(Path(self.args.infra_dir))
        self.cfg.setdefault("infra_dir", str(DEFAULT_INFRA_DIR))
        self.infra_dir = Path(self.cfg["infra_dir"])
        self.compose_dir = self.infra_dir / "ai"
        self.compose_file = self.compose_dir / "docker-compose.yml"
        self.dump_dir = self.infra_dir / "backups" / "postgres"
        self.env = parse_env(self.compose_dir / ".env")

    def save_config(self) -> None:
        if self.dry or self.check_mode:
            return
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.cfg, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(CONFIG_FILE.parent), prefix=".tmp-")
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)

    def detect_domains(self) -> dict:
        """Домены уже настроенного nginx по портам бэкендов."""
        found = {}
        sites = Path("/etc/nginx/sites-enabled")
        if not sites.is_dir():
            return found
        for conf in sites.iterdir():
            try:
                text = conf.read_text(errors="replace")
            except OSError:
                continue
            m = re.search(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)", text)
            n = re.search(r"server_name\s+([^;\s]+)", text)
            if m and n:
                kind = BACKEND_PORTS.get(m.group(1))
                if kind:
                    found.setdefault(kind, n.group(1))
        return found

    def prompt(
        self,
        store: dict,
        key: str,
        label: str,
        *,
        default: str | None = None,
        secret: bool = False,
        required: bool = False,
        hint: str | None = None,
    ) -> str:
        cur = (store.get(key) or default or "").strip()
        if self.args.yes or not sys.stdin.isatty():
            if required and not cur:
                die(f"нет значения для «{label}». Запустите без --yes и введите его.")
            store[key] = cur
            return cur
        if hint:
            print(f"    {C_DIM}{hint}{C_RESET}")
        shown = mask(cur) if secret and cur else cur
        while True:
            suffix = f" [{shown}]" if shown else ""
            try:
                if secret:
                    val = getpass.getpass(f"  {label}{suffix}: ")
                else:
                    val = input(f"  {label}{suffix}: ")
            except EOFError:
                val = ""
            val = val.strip()
            if not val:
                val = cur
            if required and not val:
                warn("значение обязательно")
                continue
            break
        store[key] = val
        return val

    def collect(self) -> None:
        """Все вопросы — до начала установки."""
        step("Параметры установки")
        dom = self.detect_domains()
        cfg = self.cfg

        print(f"  {C_DIM}Enter — оставить значение в скобках. Секреты вводятся скрыто.{C_RESET}")

        cfg["ai_domain"] = self.prompt(
            cfg, "ai_domain", "Домен Open WebUI", default=dom.get("ai", "ai.example.com"), required=True,
            hint="A-запись этого домена должна указывать на сервер (для TLS).",
        )
        cfg["llm_domain"] = self.prompt(
            cfg, "llm_domain", "Домен LiteLLM", default=dom.get("llm", "llm.example.com"), required=True
        )
        cfg["terminal_domain"] = self.prompt(
            cfg, "terminal_domain", "Домен open-terminal", default=dom.get("terminal", "bot.example.com"), required=True
        )
        email_default = cfg.get("acme_email") or f"admin@{cfg['ai_domain']}"
        cfg["acme_email"] = self.prompt(
            cfg, "acme_email", "E-mail для Let's Encrypt", default=email_default, required=True
        )

        ui_user = self.env.get("UI_USERNAME", "admin")
        self.prompt(self.env, "UI_USERNAME", "Логин администратора WebUI/LiteLLM", default=ui_user, required=True)
        self.prompt(
            self.env, "UI_PASSWORD", "Пароль администратора LiteLLM", default=secrets.token_urlsafe(18), secret=True,
            hint="Пароль админа Open WebUI задаётся при первом входе в UI.",
        )
        self.prompt(
            self.env, "OPENROUTER_API_KEY", "Ключ OpenRouter (можно пусто)", default=self.env.get("OPENROUTER_API_KEY", ""),
            secret=True, hint="openrouter.ai → Keys. Пусто — только модели с собственными ключами.",
        )

        # Telegram — необязательный блок.
        nenv = parse_env(NOTIF_DIR / ".env")
        cfg["telegram"] = cfg.get("telegram") or {}
        tg = cfg["telegram"]
        if not self.args.yes and sys.stdin.isatty():
            print(f"  {C_DIM}Telegram-уведомления: пустой токен — блок пропускается.{C_RESET}")
        tg["bot_token"] = self.prompt(
            tg, "bot_token", "Telegram BOT_TOKEN (уведомления)", default=nenv.get("BOT_TOKEN", ""), secret=True
        )
        tg["file_bot_token"] = self.prompt(
            tg, "file_bot_token", "Telegram FILE_BOT_TOKEN (файлы)", default=nenv.get("FILE_BOT_TOKEN", ""), secret=True
        )
        tg["owner"] = self.prompt(tg, "owner", "BOT_OWNER (ваш Telegram user id)", default=nenv.get("BOT_OWNER", ""))

        self.prompt(
            self.env, "GITHUB_KEY", "GitHub token для бэкапа Hermes (можно пусто)",
            default=self.env.get("GITHUB_KEY", ""), secret=True,
        )

        # Внутренние секреты — только генерируются, спрашивать нечего.
        for key in (
            "LITELLM_MASTER_KEY",
            "WEBUI_SECRET_KEY",
            "OPEN_TERMINAL_API_KEY",
            "LITELLM_DB_PASSWORD",
            "WEBUI_DB_PASSWORD",
            "SEARXNG_SECRET_KEY",
            "LITELLM_REDIS_PASSWORD",
        ):
            if not self.env.get(key) or self.env[key] == "CHANGE_ME":
                self.env[key] = secrets.token_hex(32)
                self.changed.append(f"secret {key}")

        self.env["ENABLE_ADMIN_CHAT_ACCESS"] = self.env.get("ENABLE_ADMIN_CHAT_ACCESS", "false")
        self.env["WEBUI_URL"] = f"https://{cfg['ai_domain']}"

        self.save_config()
        ok(f"Домены: {cfg['ai_domain']}, {cfg['llm_domain']}, {cfg['terminal_domain']}")
        ok(f"Каталог стека: {self.infra_dir}")

    # ── шаги ────────────────────────────────────────────────────────────────

    def step_preflight(self) -> None:
        step("Проверка окружения")
        if os.geteuid() != 0:
            die("нужен root: sudo python3 install.py")
        if not Path("/run/systemd/system").is_dir():
            die("нужен systemd (это не контейнер?)")
        if Path("/etc/os-release").is_file():
            rel = dict(
                line.strip().split("=", 1)
                for line in Path("/etc/os-release").read_text().splitlines()
                if "=" in line
            )
            os_id = rel.get("ID", "").strip('"')
            if os_id not in ("ubuntu", "debian"):
                warn(f"ОС {os_id or '?'} не Ubuntu/Debian — часть шагов может не работать")
        else:
            warn("нет /etc/os-release — ОС не определена")

        self.ensure_dir(STATE_DIR, 0o755)

        if not (ASSETS / "stack" / "docker-compose.yml").is_file():
            die(f"повреждён репозиторий: нет {ASSETS}/stack/docker-compose.yml")

        # Публичный IP и DNS — только для предупреждения, не блокируем.
        self.cfg["public_ip"] = self.public_ip()

        disk = shutil.disk_usage("/")
        free_gb = disk.free / 1024**3
        if free_gb < 10:
            warn(f"свободно {free_gb:.1f} GB на / — для образов и БД рекомендуется ≥10 GB")

        ok(f"root, systemd, {free_gb:.1f} GB свободно на /")
        if self.cfg.get("public_ip"):
            ok(f"публичный IP: {self.cfg['public_ip']}")
        self.collect()

    def public_ip(self) -> str:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
                return resp.read().decode().strip()
        except Exception:
            return ""

    def step_packages(self) -> None:
        step("Системные пакеты и Docker")
        base = [
            "curl", "ca-certificates", "gnupg", "lsb-release", "git",
            "python3", "python3-venv", "python3-pip", "rsync", "tar", "zstd", "jq",
        ]
        have = []
        for pkg in base:
            # dpkg-query быстрый и не требует обновления индексов
            res = self.run(["dpkg-query", "-W", "-f=${Status}", pkg], check=False, quiet=True)
            if res.returncode != 0 or "install ok installed" not in (res.stdout or ""):
                have.append(pkg)
        if have:
            info(f"устанавливаю: {' '.join(have)}")
            self.apt_install(have)
            ok("базовые пакеты установлены")
        else:
            ok("базовые пакеты уже есть")

        if shutil.which("docker") is None:
            info("Docker не найден — ставлю через get.docker.com")
            self.run(["bash", "-c", "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh"], stream=True, timeout=1800)
            ok("Docker установлен")
        else:
            ok(f"Docker {self.run(['docker', '--version']).stdout.strip().split()[-1].rstrip(',')}")
        self.run(["systemctl", "enable", "--now", "docker"])

        # Ротация логов на уровне демона — для контейнеров, созданных без
        # собственного logging-блока.
        daemon_json = Path("/etc/docker/daemon.json")
        desired = {
            "log-driver": "json-file",
            "log-opts": {"max-size": "50m", "max-file": "3"},
            "live-restore": True,
        }
        current = {}
        if daemon_json.is_file():
            try:
                current = json.loads(daemon_json.read_text())
            except json.JSONDecodeError:
                warn("битый /etc/docker/daemon.json — не трогаю")
                current = desired
        if current != desired and not self.dry:
            self.write_file(daemon_json, json.dumps(desired, indent=2) + "\n", 0o644)
            self.run(["systemctl", "reload", "docker"], check=False)
            ok("логи Docker ограничены 50m×3 (daemon.json)")

        res = self.run(["docker", "compose", "version"], check=False)
        if res.returncode != 0:
            info("ставлю плагин docker compose")
            self.apt_install(["docker-compose-plugin"])
            res = self.run(["docker", "compose", "version"], check=False)
            if res.returncode != 0:
                die("docker compose (v2) недоступен после установки")
        ok(f"compose {(res.stdout or '').strip().split()[-1]}")

    def step_users(self) -> None:
        step(f"Пользователь {AI_USER} и каталоги")
        res = self.run(["id", AI_USER], check=False)
        if res.returncode != 0:
            groups = "sudo"
            if self.run(["getent", "group", "docker"], check=False).returncode == 0:
                groups = "sudo,docker"
            self.run(["useradd", "-m", "-s", "/bin/bash", "-G", groups, AI_USER])
            ok(f"пользователь {AI_USER} создан")
        else:
            ok(f"пользователь {AI_USER} уже есть")
            if self.run(["getent", "group", "docker"], check=False).returncode == 0:
                self.run(["usermod", "-aG", "docker", AI_USER], check=False)

        for path, mode in [
            (self.infra_dir, 0o755),
            (self.compose_dir, 0o755),
            (self.infra_dir / "backups", 0o755),
            (self.dump_dir, 0o755),
            (WEBUI_DATA, 0o755),
            (NOTIF_DIR, 0o755),
            (STATE_DIR, 0o755),
            (Path("/var/lib/docker-autorecover"), 0o755),
            (Path("/var/lib/server-monitor"), 0o755),
            (Path("/root/open-terminal-config"), 0o700),
        ]:
            self.ensure_dir(path, mode, AI_USER if path != Path("/root/open-terminal-config") else None)

        # Каталоги юнитов, которые пишут от root.
        for path in ("/var/lib/docker-autorecover", "/var/lib/server-monitor"):
            if not self.dry:
                shutil.chown(path, "root", "root")
        ok("каталоги готовы")

    def step_stack(self) -> None:
        step("Файлы стека")
        self.ensure_dir(self.compose_dir, 0o755, AI_USER)
        n = self.sync_tree(
            ASSETS / "stack", self.compose_dir, AI_USER,
            skip=(".env", "_state_snapshot", "out", "__pycache__", ".git"),
        )
        ok(f"стек синхронизирован ({n} изменённых файлов)" if n else "стек уже актуален")

        n = self.sync_tree(
            ASSETS / "notifications", NOTIF_DIR, AI_USER, skip=(".env", "__pycache__", ".git", ".gitignore")
        )
        if n:
            ok(f"код мониторинга обновлён ({n} файлов)")

        for name in ("open-terminal-config",):
            self.sync_tree(ASSETS / "host" / name, Path("/root") / name, "root", skip=())

        if (self.infra_dir / "backup-deploy").exists():
            info("старый backup-deploy/ оставлен как есть")
        ok("файлы на месте")

    def step_secrets(self) -> None:
        step("Секреты и .env")
        tmpl = (TEMPLATES / "ai.env.tmpl").read_text()
        content = tmpl
        for key, val in self.env.items():
            content = content.replace(f"@@{key}@@", val)
        if "@@" in content:
            missing = sorted(set(re.findall(r"@@([A-Z0-9_]+)@@", content)))
            die(f"в шаблоне не заполнены: {missing}")
        env_path = self.compose_dir / ".env"
        if self.write_file(env_path, content, 0o640, "root"):
            if not self.dry:
                shutil.chown(env_path, "root", AI_USER)
            ok("ai/.env записан (0640 root:ai_service)")
        else:
            ok("ai/.env без изменений")

        tg = self.cfg.get("telegram") or {}
        nenv_path = NOTIF_DIR / ".env"
        if tg.get("bot_token"):
            t = (TEMPLATES / "notifications.env.tmpl").read_text()
            repl = {
                "BOT_TOKEN": tg.get("bot_token", ""),
                "FILE_BOT_TOKEN": tg.get("file_bot_token", ""),
                "BOT_OWNER": tg.get("owner", ""),
                "BACKUP_DIR": str(self.dump_dir),
            }
            for k, v in repl.items():
                t = t.replace(f"@@{k}@@", v)
            if self.write_file(nenv_path, t, 0o640, "root"):
                ok("notifications/.env записан")
            else:
                ok("notifications/.env без изменений")
        else:
            warn("Telegram-токен не задан — мониторинг и алерты будут пропущены")

    def fetch_bundle(self) -> Path | None:
        src = self.args.bundle
        if not src:
            local = STATE_DIR / "bundle.tar.gz"
            if local.is_file():
                return local
            return None
        if src.startswith(("http://", "https://")):
            dst = STATE_DIR / "bundle.tar.gz"
            if dst.is_file():
                info(f"bundle уже скачан: {dst}")
                return dst
            info(f"скачиваю bundle: {src}")
            self.run(["curl", "-fL", "--retry", "3", "-o", str(dst), src], timeout=3600)
            return dst
        p = Path(src).expanduser()
        if not p.is_file():
            die(f"bundle не найден: {p}")
        return p

    def step_bundle(self) -> None:
        step("Bundle с дампами")
        bundle = self.fetch_bundle()
        if not bundle:
            info("bundle не задан — восстановление только из локальных дампов")
            return
        self.cfg["bundle"] = str(bundle)
        self.save_config()
        tmp = STATE_DIR / "bundle-unpack"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        self.ensure_dir(tmp, 0o755)
        info(f"распаковываю {bundle.name}")
        try:
            with tarfile.open(bundle) as tar:
                try:
                    tar.extractall(tmp, filter="data")
                except TypeError:  # Python < 3.12
                    tar.extractall(tmp)
        except (tarfile.TarError, OSError) as exc:
            die(f"не удалось распаковать bundle: {exc}")

        dumps = sorted(tmp.rglob("*.dump"))
        if dumps:
            for dump in dumps:
                target = self.dump_dir / dump.name
                if not target.exists() or target.stat().st_size != dump.stat().st_size:
                    if not self.dry:
                        shutil.copy2(dump, target)
                        shutil.chown(target, AI_USER, AI_USER)
                    self.changed.append(str(target))
            ok(f"дампы разложены в {self.dump_dir} ({len(dumps)} шт.)")
        else:
            warn("в bundle нет *.dump")

        data = tmp / "webui_data"
        if data.is_dir():
            if any(WEBUI_DATA.iterdir()) if WEBUI_DATA.is_dir() else False:
                info("webui_data уже не пуст — данные WebUI не трогаю")
            else:
                info("восстанавливаю webui_data")
                if not self.dry:
                    self.run(["cp", "-a", f"{data}/.", str(WEBUI_DATA)])
                    self.run(["chown", "-R", f"{AI_USER}:{AI_USER}", str(WEBUI_DATA)])
                ok("webui_data восстановлен")
        if not self.dry:
            shutil.rmtree(tmp, ignore_errors=True)

    def step_postgres(self) -> None:
        step(f"PostgreSQL {PG_VERSION} + pgvector")
        cluster = Path(f"/etc/postgresql/{PG_VERSION}/main")
        need = False
        res = self.run(["psql", "--version"], check=False)
        if res.returncode != 0 or f" {PG_VERSION}." not in (res.stdout or ""):
            need = True
        if not cluster.is_dir():
            need = True

        if need:
            info(f"ставлю PostgreSQL {PG_VERSION} из pgdg")
            self.apt_install(["curl", "ca-certificates", "gnupg", "lsb-release"])
            self.ensure_dir(Path("/usr/share/postgresql-common/pgdg"), 0o755)
            self.run([
                "curl", "-fsSL", "-o",
                "/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc",
                "https://www.postgresql.org/media/keys/ACCC4CF8.asc",
            ])
            self.run(["chmod", "0644", "/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc"])
            codename = self.run(["lsb_release", "-cs"]).stdout.strip()
            Path("/etc/apt/sources.list.d/pgdg.list").write_text(
                "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] "
                f"https://apt.postgresql.org/pub/repos/apt {codename}-pgdg main\n"
            ) if not self.dry else None
            self.run(["apt-get", "update", "-qq"])
            self.apt_install([
                f"postgresql-{PG_VERSION}",
                f"postgresql-client-{PG_VERSION}",
                f"postgresql-{PG_VERSION}-pgvector",
            ])
            ok(f"PostgreSQL {PG_VERSION} установлен")
        else:
            ok(f"PostgreSQL {PG_VERSION} уже установлен")

        self.run(["systemctl", "enable", "postgresql"], check=False)
        if self.run(["systemctl", "is-active", "postgresql"], check=False).returncode != 0:
            self.run(["systemctl", "start", "postgresql"])
        self.wait_for_pg()

        conf = cluster / "postgresql.conf"
        hba = cluster / "pg_hba.conf"
        if not conf.is_file() and not self.dry:
            die(f"нет {conf}")

        changed = False
        text = conf.read_text() if conf.is_file() else ""
        if re.search(r"^[#\s]*listen_addresses\s*=", text, re.M):
            new = re.sub(r"^[#\s]*listen_addresses\s*=.*$", "listen_addresses = '*'", text, count=1, flags=re.M)
        else:
            new = text + "\nlisten_addresses = '*'\n"
        if new != text:
            self.write_file(conf, new, 0o644, "postgres")
            changed = True

        htext = hba.read_text() if hba.is_file() else ""
        if "172.16.0.0/12" not in htext:
            line = "host    all    all    172.16.0.0/12    scram-sha-256\n"
            if "# Allow replication connections" in htext:
                htext = htext.replace("# Allow replication connections", line + "# Allow replication connections", 1)
            else:
                htext += line
            self.write_file(hba, htext, 0o640, "postgres")
            changed = True

        if changed:
            self.run(["systemctl", "restart", "postgresql"])
            self.wait_for_pg()
            ok("конфигурация PostgreSQL обновлена, сервер перезапущен")
        else:
            ok("конфигурация PostgreSQL уже корректна")

    def wait_for_pg(self) -> None:
        for _ in range(30):
            if self.dry:
                return
            if self.run(["sudo", "-u", "postgres", "psql", "-tAc", "SELECT 1"], check=False, quiet=True).returncode == 0:
                return
            time.sleep(1)
        die("PostgreSQL не отвечает 30 секунд")

    def step_network(self) -> None:
        step(f"Docker-сеть {DOCKER_NET}")
        res = self.run(["docker", "network", "inspect", DOCKER_NET], check=False)
        if res.returncode != 0:
            self.run(["docker", "network", "create", DOCKER_NET])
            ok(f"сеть {DOCKER_NET} создана")
        else:
            ok(f"сеть {DOCKER_NET} уже есть")

    def image_exists(self, image: str) -> bool:
        return self.run(["docker", "image", "inspect", image], check=False).returncode == 0

    def compose_images(self) -> list[str]:
        """Список image: из compose-файла (без повторов, в порядке появления)."""
        images: list[str] = []
        if not self.compose_file.is_file():
            return images
        for line in self.compose_file.read_text().splitlines():
            m = re.match(r"\s*image:\s*(\S+)\s*$", line)
            if m and m.group(1) not in images:
                images.append(m.group(1))
        return images

    def step_images(self) -> None:
        step("Docker-образы")
        if not self.compose_file.is_file():
            die(f"нет {self.compose_file}")
        self.compose("config", "-q", quiet=True)
        ok("compose-файл валиден")

        # Базовый образ WebUI собирается локально — если его нет, собираем.
        if not self.image_exists("esmien/webui:local"):
            info("собираю esmien/webui:local (может занять несколько минут)")
            self.compose("build", "open-webui", stream=True, timeout=3600)
            ok("образ WebUI собран")
        else:
            ok("образ esmien/webui:local уже есть")

        # Остальные образы тянем только если их нет: :latest-теги не должны
        # молча обновляться при повторном запуске установщика.
        missing = False
        for img in self.compose_images():
            if img.startswith("esmien/webui"):
                continue
            if not self.image_exists(img):
                info(f"тяну {img}")
                self.run(["docker", "pull", img], stream=True, timeout=3600)
                missing = True
        ok("образы на месте" if not missing else "образы загружены")

    def ensure_roles_and_databases(self) -> None:
        step("Роли и базы данных")
        roles = {
            "webui": self.env["WEBUI_DB_PASSWORD"],
            "litellm": self.env["LITELLM_DB_PASSWORD"],
        }
        for role, pw in roles.items():
            sql = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} LOGIN PASSWORD {sql_lit(pw)};
  ELSE
    ALTER ROLE {role} WITH LOGIN PASSWORD {sql_lit(pw)};
  END IF;
END
$$;
"""
            self.psql(sql)
        ok("роли webui и litellm созданы/обновлены")

        for db, owner in (("webui_db", "webui"), ("litellm_db", "litellm")):
            exists = self.psql_scalar(f"SELECT 1 FROM pg_database WHERE datname = '{db}'")
            if exists != "1":
                self.psql(f'CREATE DATABASE "{db}" OWNER "{owner}";')
                ok(f"создана база {db}")
            else:
                info(f"база {db} уже есть")
            self.psql("CREATE EXTENSION IF NOT EXISTS vector;", db=db)
        ok("расширение vector включено")

    def step_migrate(self) -> None:
        step("Миграции LiteLLM")
        self.compose("up", "-d", "litellm-redis", "litellm", stream=True, timeout=1200)
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.psql_scalar(
                "SELECT 1 FROM pg_tables WHERE tablename = 'LiteLLM_VerificationToken'", db="litellm_db"
            ) == "1":
                break
            if self.run(["docker", "ps", "--filter", "name=^litellm$", "--format", "{{.Names}}"]).stdout.strip() == "":
                self.compose("logs", "--tail=40", "litellm", check=False, quiet=False)
                die("litellm упал во время миграций")
            time.sleep(5)
        else:
            self.compose("logs", "--tail=40", "litellm", check=False, quiet=False)
            die("миграции LiteLLM не завершились за 180 с")
        ok("схема LiteLLM готова")

    def step_restore(self) -> None:
        step("Восстановление БД")
        if self.args.skip_restore:
            warn("--skip-restore: БД не трогаю")
            return

        restore_all = bool(self.args.restore)
        bundle = self.cfg.get("bundle")
        dbs = [("litellm_db", "litellm"), ("webui_db", "webui")]
        todo = []
        for db, owner in dbs:
            tables = self.psql_scalar("SELECT count(*) FROM pg_tables WHERE schemaname='public'", db=db)
            dump = self.find_dump(db)
            if not dump:
                info(f"{db}: дамп не найден — пропускаю")
                continue
            age_h = (time.time() - dump.stat().st_mtime) / 3600
            if age_h > STALE_HOURS and not self.args.force_stale and not bundle:
                warn(f"{db}: дамп {dump.name} старше {STALE_HOURS} ч ({age_h:.0f} ч) — пропускаю (--force-stale чтобы применить)")
                continue
            if tables not in ("0", "") and not restore_all:
                info(f"{db}: в базе уже {tables} таблиц — восстановление пропущено (--restore чтобы перезаписать)")
                continue
            todo.append((db, owner, dump))

        if not todo:
            ok("восстанавливать нечего")
            return

        for db, owner, dump in todo:
            info(f"{db} ← {dump.name} ({dump.stat().st_size / 1024**2:.1f} MB)")
            log = STATE_DIR / f"restore-{db}.log"
            with log.open("w") as lf:
                proc = subprocess.run(
                    ["sudo", "-u", "postgres", "pg_restore", f"--dbname={db}", "--clean", "--if-exists",
                     "--no-owner", "--no-privileges", "--single-transaction", "--verbose"],
                    stdin=dump.open("rb"), stdout=lf, stderr=subprocess.STDOUT,
                ) if not self.dry else subprocess.CompletedProcess([], 0)
            if proc.returncode != 0:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-10:])
                print(tail, file=sys.stderr)
                die(f"pg_restore {db} завершился с кодом {proc.returncode}")
            self.psql("CREATE EXTENSION IF NOT EXISTS vector;", db=db, check=False)
            self.reassign_ownership(db, owner)
            ok(f"{db} восстановлена, владение → {owner}")

        self.create_views()
        # LiteLLM должен перечитать схему после подмены БД.
        self.compose("restart", "litellm", stream=False, timeout=300)

    def find_dump(self, db: str) -> Path | None:
        candidates: list[Path] = []
        for d in (self.dump_dir, self.infra_dir / "backups"):
            if d.is_dir():
                candidates += [p for p in d.glob(f"{db}_*.dump") if p.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def reassign_ownership(self, db: str, owner: str) -> None:
        self.psql(
            f"""
SELECT format('ALTER TABLE public.%I OWNER TO %I', tablename, '{owner}') FROM pg_tables WHERE schemaname='public'
\\gexec
SELECT format('ALTER SEQUENCE public.%I OWNER TO %I', sequencename, '{owner}') FROM pg_sequences WHERE schemaname='public'
\\gexec
SELECT format('ALTER VIEW public.%I OWNER TO %I', viewname, '{owner}') FROM pg_views WHERE schemaname='public'
\\gexec
SELECT format('ALTER MATERIALIZED VIEW public.%I OWNER TO %I', matviewname, '{owner}') FROM pg_matviews WHERE schemaname='public'
\\gexec
GRANT ALL ON SCHEMA public TO {owner};
GRANT ALL ON ALL TABLES IN SCHEMA public TO {owner};
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {owner};
""",
            db=db,
            check=False,
        )

    def create_views(self) -> None:
        step("Аналитические view LiteLLM")
        self.psql(
            """
CREATE OR REPLACE VIEW "LiteLLM_VerificationTokenView" AS
SELECT v.*, t.spend AS team_spend, t.max_budget AS team_max_budget,
       t.model_max_budget AS team_model_max_budget,
       t.tpm_limit AS team_tpm_limit, t.rpm_limit AS team_rpm_limit,
       t.tpd_limit AS team_tpd_limit, p.project_alias AS project_alias
FROM "LiteLLM_VerificationToken" v
LEFT JOIN "LiteLLM_TeamTable"    t ON v.team_id    = t.team_id
LEFT JOIN "LiteLLM_ProjectTable" p ON v.project_id = p.project_id;

CREATE OR REPLACE VIEW "MonthlyGlobalSpend" AS
SELECT DATE("startTime") AS date, SUM("spend") AS spend
FROM "LiteLLM_SpendLogs"
WHERE "startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE("startTime");

CREATE OR REPLACE VIEW "Last30dKeysBySpend" AS
SELECT L."api_key", V."key_alias", V."key_name", SUM(L."spend") AS total_spend
FROM "LiteLLM_SpendLogs" L
LEFT JOIN "LiteLLM_VerificationToken" V ON L."api_key" = V."token"
WHERE L."startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY L."api_key", V."key_alias", V."key_name"
ORDER BY total_spend DESC;

CREATE OR REPLACE VIEW "Last30dModelsBySpend" AS
SELECT "model", SUM("spend") AS total_spend
FROM "LiteLLM_SpendLogs"
WHERE "startTime" >= CURRENT_DATE - INTERVAL '30 days' AND "model" <> ''
GROUP BY "model" ORDER BY total_spend DESC;

CREATE OR REPLACE VIEW "MonthlyGlobalSpendPerKey" AS
SELECT DATE("startTime") AS date, SUM("spend") AS spend, api_key
FROM "LiteLLM_SpendLogs"
WHERE "startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE("startTime"), api_key;

CREATE OR REPLACE VIEW "MonthlyGlobalSpendPerUserPerKey" AS
SELECT DATE("startTime") AS date, SUM("spend") AS spend, api_key, "user"
FROM "LiteLLM_SpendLogs"
WHERE "startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE("startTime"), "user", api_key;

CREATE OR REPLACE VIEW "DailyTagSpend" AS
SELECT jsonb_array_elements_text(request_tags) AS individual_request_tag,
       DATE(s."startTime") AS spend_date,
       COUNT(*) AS log_count,
       SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs" s
GROUP BY individual_request_tag, DATE(s."startTime");

CREATE OR REPLACE VIEW "Last30dTopEndUsersSpend" AS
SELECT end_user, COUNT(*) AS total_events, SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs"
WHERE end_user <> '' AND end_user <> 'user'
  AND "startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY end_user ORDER BY total_spend DESC LIMIT 100;
""",
            db="litellm_db",
        )
        self.reassign_ownership("litellm_db", "litellm")
        ok("view пересозданы")

    def step_apps(self) -> None:
        step("Запуск стека")
        self.compose("up", "-d", stream=True, timeout=1800)
        expected = {
            "open-webui": 240,
            "litellm": 180,
            "searxng": 120,
            "open-terminal": 120,
            "litellm-redis": 60,
        }
        for name, limit in expected.items():
            self.wait_healthy(name, limit)
        ok("все контейнеры здоровы")

        # Права на данные WebUI (контейнер пишет от root).
        if WEBUI_DATA.is_dir() and not self.dry:
            shutil.chown(WEBUI_DATA, AI_USER, AI_USER)

    def wait_healthy(self, name: str, limit: int) -> None:
        deadline = time.time() + limit
        last = ""
        while time.time() < deadline:
            if self.dry:
                return
            res = self.run(
                ["docker", "inspect", name, "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"],
                check=False,
            )
            last = (res.stdout or "").strip()
            if last in ("healthy", "running"):
                ok(f"{name}: {last}")
                return
            time.sleep(5)
        self.compose("logs", "--tail=30", name, check=False, quiet=False)
        self.failed.append(f"{name} не стал healthy за {limit}s (state={last})")
        warn(f"{name} не стал healthy за {limit}s (state={last})")

    def step_notifications(self) -> None:
        step("Telegram-мониторинг")
        tg = self.cfg.get("telegram") or {}
        if not tg.get("bot_token"):
            warn("нет BOT_TOKEN — пропускаю")
            return
        req = NOTIF_DIR / "requirements.txt"
        if not req.is_file():
            warn("нет requirements.txt — пропускаю установку зависимостей")
            return
        pip = ["python3", "-m", "pip", "install", "--break-system-packages", "-q", "-r", str(req)]
        res = self.run(pip, check=False, timeout=900)
        if res.returncode != 0:
            # Ubuntu без --break-system-packages (22.04) — пробуем без флага.
            self.run(["python3", "-m", "pip", "install", "-q", "-r", str(req)], timeout=900)
        ok("зависимости мониторинга установлены")

    def install_units(self, units: list[str], enable: list[str]) -> None:
        src_units = ASSETS / "host" / "systemd"
        for unit in units:
            src = src_units / unit
            if not src.is_file():
                warn(f"нет файла юнита {unit}")
                continue
            self.copy_file(src, Path("/etc/systemd/system") / unit, 0o644)
        self.run(["systemctl", "daemon-reload"])
        for unit in enable:
            self.run(["systemctl", "enable", "--now", unit], check=False)

    def step_units(self) -> None:
        step("systemd-юниты и таймеры")
        for name in HOST_BIN_SCRIPTS:
            src = ASSETS / "host" / "bin" / name
            if src.is_file():
                self.copy_file(src, Path("/usr/local/bin") / name, 0o755)
        ok("скрипты в /usr/local/bin")

        units = CORE_TIMERS + CORE_SERVICES
        enable = list(CORE_TIMERS) + list(CORE_SERVICES)
        tg = self.cfg.get("telegram") or {}
        if tg.get("bot_token"):
            units += NOTIFY_SERVICES
            enable += NOTIFY_SERVICES
        self.install_units(units, enable)
        ok(f"юниты включены ({len(enable)} шт.)")

        # Разовый прогон бэкапа, чтобы проверить, что он работает.
        if not self.dry and (self.compose_dir / ".env").is_file():
            res = self.run(["systemctl", "start", "postgres-backup.service"], check=False, timeout=1800)
            if res.returncode == 0:
                ok("тестовый бэкап БД выполнен")
            else:
                warn("тестовый бэкап не удался — см. journalctl -u postgres-backup")

    # ── TLS ─────────────────────────────────────────────────────────────────

    def vhost_http(self, domain: str, backend: tuple[str, int]) -> str:
        host, port = backend
        return f"""# {domain} -> {host}:{port}
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{
        proxy_pass http://{host}:{port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""

    def vhost_ssl(self, domain: str, backend: tuple[str, int]) -> str:
        host, port = backend
        return f"""# {domain} -> {host}:{port}
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    client_max_body_size 100M;

    location / {{
        proxy_pass http://{host}:{port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""

    def dns_points_here(self, domain: str) -> bool:
        try:
            resolved = socket.gethostbyname(domain)
        except socket.gaierror:
            return False
        public = self.cfg.get("public_ip") or self.public_ip()
        return bool(public) and resolved == public

    def step_tls(self) -> None:
        step("Nginx + сертификаты")
        if self.args.skip_tls:
            warn("--skip-tls: nginx/certbot не трогаю")
            return
        if shutil.which("nginx") is None:
            self.apt_install(["nginx"])
            ok("nginx установлен")
        else:
            ok("nginx уже установлен")
        self.run(["systemctl", "enable", "nginx"], check=False)
        self.ensure_dir(Path("/var/www/html"), 0o755)

        sites = {
            self.cfg["ai_domain"]: ("127.0.0.1", 8080),
            self.cfg["llm_domain"]: ("127.0.0.1", 4000),
            self.cfg["terminal_domain"]: ("127.0.0.1", 8000),
        }
        avail = Path("/etc/nginx/sites-available")
        enabled = Path("/etc/nginx/sites-enabled")
        self.ensure_dir(avail, 0o755)
        self.ensure_dir(enabled, 0o755)

        to_issue = []
        for domain, backend in sites.items():
            cert = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")
            conf = avail / domain
            if cert.is_file():
                content = self.vhost_ssl(domain, backend)
            else:
                content = self.vhost_http(domain, backend)
                to_issue.append(domain)
            self.write_file(conf, content, 0o644)
            link = enabled / domain
            if not link.exists() and not self.dry:
                link.symlink_to(f"../sites-available/{domain}")
            info(f"{domain} → {backend[1]}")

        default_site = enabled / "default"
        if default_site.exists() or default_site.is_symlink():
            if not self.dry:
                default_site.unlink()
            info("дефолтный vhost отключён")

        self.run(["nginx", "-t"])
        self.run(["systemctl", "reload", "nginx"])
        ok(f"vhost'ы настроены: {len(sites)}")

        if not to_issue:
            ok("сертификаты уже выпущены")
            self.ensure_renew_timer()
            return

        if shutil.which("certbot") is None:
            self.apt_install(["certbot", "python3-certbot-nginx"])
            ok("certbot установлен")

        issued = []
        for domain in to_issue:
            if not self.dns_points_here(domain):
                warn(f"{domain} не указывает на этот сервер — сертификат не выпускаю (HTTP-конфиг оставлен)")
                continue
            info(f"выпускаю сертификат для {domain}")
            res = self.run(
                ["certbot", "--nginx", "--non-interactive", "--agree-tos", "--redirect",
                 "--keep-until-expiring", "-m", self.cfg["acme_email"], "-d", domain],
                check=False, timeout=600, quiet=False,
            )
            if res.returncode == 0:
                issued.append(domain)
                self.write_file(avail / domain, self.vhost_ssl(domain, sites[domain]), 0o644)
            else:
                warn(f"certbot не смог выпустить сертификат для {domain}")

        self.run(["nginx", "-t"], check=False)
        self.run(["systemctl", "reload", "nginx"], check=False)
        if issued:
            ok(f"сертификаты выпущены: {', '.join(issued)}")
        self.ensure_renew_timer()

    def ensure_renew_timer(self) -> None:
        res = self.run(["systemctl", "cat", "certbot.timer"], check=False)
        if res.returncode == 0:
            self.run(["systemctl", "enable", "--now", "certbot.timer"], check=False)
            ok("certbot.timer активен")
        else:
            ok("certbot renew идёт через cron пакета")

    # ── проверка ────────────────────────────────────────────────────────────

    def step_verify(self) -> None:
        step("Проверка результата")
        healthy = True
        for name in ("litellm-redis", "litellm", "open-webui", "searxng", "open-terminal"):
            res = self.run(
                ["docker", "inspect", name, "--format",
                 "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}"],
                check=False,
            )
            if res.returncode != 0:
                print(f"  {C_RED}✗{C_RESET} {name}: не найден")
                healthy = False
                continue
            parts = (res.stdout or "").strip().split("|")
            state = parts[0]
            health = parts[1] if len(parts) > 1 else ""
            restarts = parts[2] if len(parts) > 2 else "?"
            good = state == "running" and health in ("healthy", "")
            mark = f"{C_GREEN}✓{C_RESET}" if good else f"{C_RED}✗{C_RESET}"
            print(f"  {mark} {name}: {state} {health} restarts={restarts}")
            healthy = healthy and good

        for db in ("webui_db", "litellm_db"):
            n = self.psql_scalar("SELECT count(*) FROM pg_tables WHERE schemaname='public'", db=db)
            print(f"  {C_DIM}·{C_RESET} {db}: {n or '?'} таблиц")

        checks = [
            ("litellm", "http://127.0.0.1:4000/health/liveliness"),
            ("open-webui", "http://127.0.0.1:8080/health"),
            ("searxng", "http://127.0.0.1:8081/healthz"),
            ("open-terminal", "http://127.0.0.1:8000/health"),
        ]
        for name, url in checks:
            res = self.run(["curl", "-fsS", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", url], check=False)
            print(f"  {C_DIM}·{C_RESET} {name}: HTTP {res.stdout.strip() or 'ERR'}")

        if not self.args.skip_tls:
            for domain in (self.cfg.get("ai_domain"), self.cfg.get("llm_domain"), self.cfg.get("terminal_domain")):
                if not domain:
                    continue
                res = self.run(
                    ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-m", "10", f"https://{domain}"],
                    check=False,
                )
                code = res.stdout.strip() or "ERR"
                print(f"  {C_DIM}·{C_RESET} https://{domain}: {code}")

        active = self.run(["systemctl", "list-timers", "--no-legend", "--plain"], check=False).stdout or ""
        marks = [u for u in CORE_TIMERS if u in active]
        print(f"  {C_DIM}·{C_RESET} таймеров активно: {len(marks)}/{len(CORE_TIMERS)}")

        if not healthy:
            self.failed.append("не все контейнеры здоровы")
        if healthy:
            ok("стек здоров")
        else:
            warn("есть проблемы — см. docker compose logs")

    def step_hermes(self) -> None:
        step("Hermes")
        if self.args.hermes is False:
            info("--no-hermes: пропускаю")
            return
        installed = shutil.which("hermes") is not None
        if self.args.hermes is None:
            if self.args.yes or not sys.stdin.isatty():
                info("Hermes не ставится без подтверждения (--hermes)")
                return
            answer = input(
                f"\n  {C_BOLD}Установить Hermes в хостовом режиме?{C_RESET} "
                f"{C_DIM}[y/N]{C_RESET}: "
            ).strip().lower()
            if answer not in ("y", "yes", "д", "да"):
                info("пропущено — поставить позже: python3 install.py --only hermes --hermes")
                return
        if installed:
            ok(f"Hermes уже установлен: {shutil.which('hermes')}")
        else:
            info("запускаю официальный установщик Hermes (интерактивный)")
            if self.dry:
                info(f"[dry-run] curl -fsSL {HERMES_INSTALL_URL} | bash")
                return
            proc = subprocess.run(["bash", "-c", f"curl -fsSL {HERMES_INSTALL_URL} | bash"])
            if proc.returncode != 0:
                warn("установщик Hermes завершился с ошибкой — инфраструктура при этом готова")
                return
            ok("Hermes установлен")

        # Обвязка Hermes: boot-notify + бэкап состояния.
        expected = Path("/etc/hermes-boot-expected-containers.txt")
        if not expected.is_file() and not self.dry:
            expected.write_text("\n".join(["litellm", "open-webui", "searxng", "open-terminal", "litellm-redis"]) + "\n")
            self.install_units(["hermes-boot-notify.service"], ["hermes-boot-notify.service"])
        if self.env.get("GITHUB_KEY"):
            self.install_units(HERMES_UNITS, ["hermes-boot-notify.service", "hermes-state-watch.service",
                                              "hermes-state-backup.timer"])
            ok("обвязка Hermes включена (boot-notify, state backup)")
        else:
            info("GITHUB_KEY не задан — бэкап состояния Hermes не настроен")

    # ── запуск ──────────────────────────────────────────────────────────────

    def selected_steps(self) -> list[tuple[str, str]]:
        steps = STEPS
        if self.args.only:
            want = {s.strip() for s in self.args.only.split(",") if s.strip()}
            unknown = want - set(STEP_NAMES)
            if unknown:
                die(f"неизвестные шаги: {', '.join(sorted(unknown))}")
            steps = [s for s in steps if s[0] in want]
        if self.args.skip:
            drop = {s.strip() for s in self.args.skip.split(",") if s.strip()}
            unknown = drop - set(STEP_NAMES)
            if unknown:
                die(f"неизвестные шаги: {', '.join(sorted(unknown))}")
            steps = [s for s in steps if s[0] not in drop]
            if any(s[0] in ("stack", "secrets") for s in steps) and "preflight" in drop:
                info("preflight пропущен — конфигурация берётся из существующих файлов")
        return steps

    def execute(self) -> int:
        self.open_log()
        self.load_config()
        steps = self.selected_steps()
        t0 = time.time()
        for name, title in steps:
            fn = getattr(self, f"step_{name}")
            try:
                fn()
            except InstallError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log(traceback.format_exc())
                die(f"шаг «{name}» упал: {exc}")
        elapsed = time.time() - t0

        state = {
            "last_run": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "steps": [s[0] for s in steps],
            "changed": len(self.changed),
            "failed": self.failed,
            "duration_sec": round(elapsed, 1),
        }
        if not self.dry:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")

        print()
        if self.failed:
            warn(f"завершено за {elapsed:.0f} c, есть проблемы: {'; '.join(self.failed)}")
            return 2
        ok(f"готово за {elapsed:.0f} c")
        if self.dry:
            info("это был --dry-run, изменений нет")
        return 0


# ── bundle (экспорт для переезда) ───────────────────────────────────────────


def do_export(args: argparse.Namespace) -> int:
    infra = Path(args.infra_dir or DEFAULT_INFRA_DIR)
    dump_dir = infra / "backups" / "postgres"
    out_dir = Path(args.out or STATE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"vps-infra-bundle-{stamp}.tar.gz"

    files: list[tuple[Path, str]] = []
    for db in ("webui_db", "litellm_db"):
        candidates = list(dump_dir.glob(f"{db}_*.dump")) + list((infra / "backups").glob(f"{db}_*.dump"))
        if not candidates:
            warn(f"нет дампа {db}")
            continue
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        age_h = (time.time() - latest.stat().st_mtime) / 3600
        if age_h > 48:
            warn(f"{db}: дампу {age_h:.0f} ч — можно обновить: systemctl start postgres-backup")
        files.append((latest, latest.name))
        print(f"  {C_DIM}·{C_RESET} {db}: {latest.name} ({latest.stat().st_size / 1024**2:.1f} MB)")

    with tarfile.open(out, "w:gz") as tar:
        for path, arcname in files:
            tar.add(path, arcname=arcname)
        if args.with_data:
            data = WEBUI_DATA
            if data.is_dir():
                print(f"  {C_DIM}·{C_RESET} webui_data: {sum(1 for _ in data.rglob('*'))} объектов")
                tar.add(data, arcname="webui_data")
        manifest = {
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": socket.gethostname(),
            "dumps": [arc for _, arc in files],
            "webui_data": bool(args.with_data),
        }
        blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(blob)
        ti.mode = 0o644
        import io

        tar.addfile(ti, io.BytesIO(blob))

    print()
    ok(f"bundle: {out} ({out.stat().st_size / 1024**2:.1f} MB)")
    info("перенесите файл на новый сервер и запустите:")
    info(f"  install.py --bundle {out.name} --restore")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Установщик инфраструктуры VPS (идемпотентный).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Шаги: " + ", ".join(STEP_NAMES) + "\n"
            "Примеры:\n"
            "  sudo python3 install.py\n"
            "  sudo python3 install.py --yes\n"
            "  sudo python3 install.py check\n"
            "  sudo python3 install.py --only stack,units,verify\n"
            "  sudo python3 install.py --bundle bundle.tar.gz --restore\n"
            "  sudo python3 install.py export --with-data\n"
        ),
    )
    p.add_argument("--infra-dir", help=f"каталог стека (по умолчанию {DEFAULT_INFRA_DIR})")
    p.add_argument("--yes", "-y", action="store_true", help="без вопросов: существующие значения/дефолты")
    p.add_argument("--reconfigure", action="store_true", help="переспросить параметры (Enter = оставить)")
    p.add_argument("--only", help="выполнить только шаги (через запятую)")
    p.add_argument("--skip", help="пропустить шаги (через запятую)")
    p.add_argument("--bundle", help="архив с дампами БД (путь или URL)")
    p.add_argument("--restore", action="store_true", help="восстановить БД поверх существующих данных")
    p.add_argument("--force-stale", action="store_true", help=f"применять дампы старше {STALE_HOURS} ч")
    p.add_argument("--skip-restore", action="store_true", help="не трогать базы")
    p.add_argument("--skip-tls", action="store_true", help="не трогать nginx/certbot")
    p.add_argument("--hermes", dest="hermes", action="store_true", default=None, help="установить Hermes")
    p.add_argument("--no-hermes", dest="hermes", action="store_false", help="не устанавливать Hermes")
    p.add_argument("--dry-run", action="store_true", help="показать план, ничего не менять")
    p.add_argument("--out", help="каталог для export")
    p.add_argument("--with-data", action="store_true", help="export: включить webui_data")
    p.add_argument("command", nargs="?", default="install", choices=["install", "check", "export"])
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "export":
        return do_export(args)

    if args.command == "check":
        args.check_mode = True
        args.yes = True
        args.skip = "packages,users,stack,secrets,bundle,postgres,network,images,migrate,restore,apps,notifications,units,tls,hermes"
        args.only = "preflight,verify"

    inst = Installer(args)
    try:
        return inst.execute()
    except InstallError as exc:
        print(f"\n{C_RED}{C_BOLD}ОШИБКА:{C_RESET} {exc}", file=sys.stderr)
        if inst.log_handle:
            inst.log(f"FAILED: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
