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
  --from-bot              взять последний бэкап БД из Telegram-бота (спросит токен)
  --tg-registry PATH|URL  реестр file_id (по умолчанию — приватный hermes-state)
  --secrets-from-bot      взять архив .env из файл-бота (набор secrets_offsite)
  --secrets-archive PATH  локальный .enc архив секретов вместо скачивания
  --secrets-key PATH      пароль архива (по умолчанию /root/.secrets/offsite.key)
  --bot-token STR         переопределить BOT_TOKEN (тестовый бот для прогона)
  --file-bot-token STR    переопределить FILE_BOT_TOKEN
  --set KEY=VALUE         переопределить значение .env (можно несколько раз)
  --restore               восстановить БД даже поверх существующих данных
  --skip-tls              не трогать nginx/certbot
  --hermes / --no-hermes  установить/не устанавливать Hermes
  --restore-hermes        развернуть config/skills/скрипты Hermes из hermes-state
  --dry-run               показать, что будет сделано
"""

from __future__ import annotations

import argparse
import calendar
import collections
import getpass
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import traceback
import tty
import urllib.error
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

# Приватный репозиторий состояния Hermes: конфиги/скиллы + реестр file_id дампов.
HERMES_STATE_REPO = os.environ.get("HERMES_STATE_REPO", "Esmien/hermes-state")
HERMES_STATE_BRANCH = os.environ.get("HERMES_STATE_BRANCH", "main")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))
# Bot API отдаёт файл через getFile только до 20 MB — части больше этого скачать нельзя.
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TG_API = "https://api.telegram.org"
# Заглушка, которой save_config маскирует токены в install.json. Обратный путь запрещён:
# однажды install.py --yes записал её как настоящий токен в notifications/.env и убил ботов.
MASKED_TOKEN = "__stored_in_notifications_env__"

# ── цвета / вывод ────────────────────────────────────────────────────────────

C_RESET = "\033[0m"
C_RED = "\033[1;31m"
C_GREEN = "\033[1;32m"
C_YELLOW = "\033[1;33m"
C_CYAN = "\033[1;36m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"


class Pane:
    """Живой блок вывода под заголовком шага.

    Заголовок шага и разделитель с таймером остаются на месте, а ниже
    прокручивается окно последних строк (как хвост лога). При смене шага
    окно стирается, заголовок занимает его место. Всё это только при tty:
    в pipe/лог уходит обычный построчный вывод.
    """

    def __init__(self) -> None:
        self.enabled = sys.stdout.isatty() and not os.environ.get("NO_PANE")
        size = shutil.get_terminal_size((100, 40))
        self.cols = max(40, size.columns)
        self.height = max(6, min(18, size.lines - 14))
        self.buf: collections.deque[str] = collections.deque(maxlen=self.height)
        self.drawn = False
        self.title = ""
        self.started = 0.0
        self._last = 0.0

    @staticmethod
    def _plain(text: str) -> str:
        return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", str(text))

    def _fit(self, text: str) -> str:
        line = self._plain(text).replace("\t", "  ").rstrip()
        return line[: self.cols - 2]

    def _divider(self) -> str:
        elapsed = int(time.time() - self.started) if self.started else 0
        stamp = f"{elapsed // 60}:{elapsed % 60:02d}"
        width = max(10, self.cols - len(stamp) - 4)
        return f"{C_DIM}{'─' * width} {stamp}{C_RESET}"

    def _frame(self) -> None:
        header = f"{C_CYAN}{C_BOLD}==> {self.title}{C_RESET}"
        lines = list(self.buf)
        out = ["\r\033[2K" + header + "\n", "\r\033[2K" + self._divider() + "\n"]
        for i in range(self.height):
            out.append("\r\033[2K" + (self._fit(lines[i]) if i < len(lines) else ""))
            if i < self.height - 1:
                out.append("\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn = True

    def begin(self, title: str) -> None:
        self.title = title
        self.started = time.time()
        self.buf.clear()
        self._last = 0.0
        if not self.enabled:
            print(f"\n{C_CYAN}{C_BOLD}==> {title}{C_RESET}")
            return
        if self.drawn:
            # встаём на строку разделителя: заголовок предыдущего шага остаётся выше
            sys.stdout.write(f"\r\033[{self.height}A\033[J")
        else:
            sys.stdout.write("\n")
        self._frame()

    def push(self, text: str) -> None:
        if not self.enabled or not self.drawn:
            print(text)
            return
        for line in str(text).splitlines() or [""]:
            self.buf.append(line)
        sys.stdout.write(f"\r\033[{self.height + 1}A")
        self._frame()

    def finish(self) -> None:
        if not self.enabled or not self.drawn:
            return
        sys.stdout.write(f"\r\033[{self.height}A\033[J")
        sys.stdout.flush()
        self.drawn = False


PANE = Pane()


def say(text: str = "") -> None:
    """Любая информационная строка: в панель, если она активна."""
    PANE.push(text)


def step(title: str) -> None:
    PANE.begin(title)


def ok(msg: str) -> None:
    PANE.push(f"  {C_GREEN}✓{C_RESET} {msg}")


def info(msg: str) -> None:
    PANE.push(f"  {C_DIM}·{C_RESET} {msg}")


def warn(msg: str) -> None:
    PANE.push(f"  {C_YELLOW}!{C_RESET} {msg}")


class InstallError(RuntimeError):
    pass


def die(msg: str) -> None:
    PANE.finish()
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

# Что тянем из Telegram-бота: две БД и архив пользовательских файлов WebUI.
RESTORE_DBS = ("webui_db", "litellm_db", "webui_data")

STEPS: list[tuple[str, str]] = [
    ("preflight", "Проверка окружения и сбор параметров"),
    ("packages", "Системные пакеты и Docker"),
    ("users", "Пользователь ai_service и каталоги"),
    ("stack", "Файлы стека"),
    ("secrets", "Секреты и .env"),
    ("bundle", "Bundle с дампами"),
    ("fetch", "Дампы из Telegram-бота"),
    ("postgres", "PostgreSQL 17 + pgvector"),
    ("network", "Docker-сеть"),
    ("images", "Docker-образы"),
    ("migrate", "Роли, базы и миграции LiteLLM"),
    ("restore", "Восстановление БД"),
    ("data", "Файлы WebUI из Telegram-бота"),
    ("apps", "Запуск стека"),
    ("notifications", "Telegram-мониторинг"),
    ("beszel", "Beszel (графики и пороги)"),
    ("units", "systemd-юниты и таймеры"),
    ("hardening", "fail2ban и anti-flood"),
    ("tls", "Nginx + сертификаты"),
    ("verify", "Проверка результата"),
    ("health", "Проверка доступности сервисов"),
    ("hermes", "Hermes"),
    ("postcheck", "Пост-проверка Hermes (таймер)"),
    ("report", "Отчёт с секретами"),
]
STEP_NAMES = [s[0] for s in STEPS]

# Юниты, которые ставит установщик (из assets/host/systemd).
CORE_TIMERS = [
    "deadman-ping.timer",
    "uptime-heartbeat.timer",
    "docker-autorecover.timer",
    "docker-detector-liveness.timer",
    "litellm-spend-retention.timer",
    "litellm-watchdog.timer",
    "open-terminal-restart.timer",
    "postgres-backup.timer",
    "postgres-backup-notify.timer",
    "searxng-quality.timer",
    "webui-compact-images.timer",
    "webui-data-backup.timer",
    "webui-generated-images-retention.timer",
    "hermes-history-backup.timer",
    "secrets-offsite-backup.timer",
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
    "hermes-task-digest.service",
    "hermes-task-board-watch.service",
    "hermes-task-board-watch.path",
]

HOST_BIN_SCRIPTS = [
    "deadman-ping.sh",
    "uptime-heartbeat.sh",
    "docker-detector-liveness.sh",
    "docker-incident-agent.sh",
    "docker-incident-detector.py",
    "fw-guard-postgres.sh",
    "litellm-spend-retention.sh",
    "litellm-watchdog.py",
    "litellm-incident-agent.sh",
    "open-terminal-restart.sh",
    "owui-signup-approver.py",
    "searxng-quality-check.py",
    "webui-compact-images.sh",
    "webui-data-backup.py",
    "webui-data-restore.py",
    "webui-generated-images-retention.sh",
    "hermes-boot-notify.sh",
    "hermes-state-backup.sh",
    "hermes-state-watch.sh",
    "hermes-history-backup.py",
    "secrets-offsite-backup.py",
    "task-board-digest.py",
    "task-board-watch.py",
]

BACKEND_PORTS = {"8080": "ai", "4000": "llm"}

# fail2ban: SSH-брутфорс. Конфиг кладётся в /etc/fail2ban/jail.local
# (перекрывает jail.conf и jail.d/defaults-debian.conf, где sshd уже включён).
FAIL2BAN_JAIL = """[DEFAULT]
# Не банить себя и внутренние сети (docker)
ignoreip = 127.0.0.1/8 ::1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16

bantime  = 1d
findtime = 1h
maxretry = 4

# Повторные нарушители получают экспоненциально дольше
bantime.increment = true
bantime.factor    = 2
bantime.maxtime   = 4w

backend = systemd

[sshd]
enabled  = true
mode     = aggressive
port     = ssh
logpath  = %(sshd_log)s
maxretry = 4
"""

# nginx: зоны rate-limit. Подключается из conf.d (раньше sites-enabled).
# На 24.04 у fail2ban action = nftables (своя таблица inet f2b-table),
# это в отдельном пространстве и с iptables-правилами fw-guard не конфликтует.
RATE_LIMIT_CONF = """# Rate limiting — защита от L7-флуда (статика WebUI намеренно без лимита)
# llm: публичный API LiteLLM, ai: /api/ Open WebUI (вход + чат + загрузки)
limit_req_zone $binary_remote_addr zone=llm_api:10m  rate=10r/s;
limit_req_zone $binary_remote_addr zone=webui_api:10m rate=8r/s;
limit_conn_zone $binary_remote_addr zone=conn_per_ip:10m;
"""


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
        # Telegram-восстановление и состояние Hermes: токен живёт только в памяти.
        self.bot_restore = bool(getattr(args, "from_bot", False))
        self.tg_token = getattr(args, "tg_token", "") or ""
        self.hermes_restore: bool | None = getattr(args, "restore_hermes", None)
        self.dump_from_bot = False

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
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=full_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                killed = {"flag": False}
                if timeout:
                    def _killer() -> None:
                        killed["flag"] = True
                        proc.kill()

                    timer = threading.Timer(timeout, _killer)
                    timer.daemon = True
                    timer.start()
                else:
                    timer = None
                assert proc.stdout is not None
                for line in proc.stdout:
                    plain = line.rstrip()
                    if plain:
                        self.log(plain)
                        PANE.push(plain)
                rc = proc.wait()
                if timer:
                    timer.cancel()
                if killed["flag"]:
                    die(f"команда не завершилась за {timeout}s: {printable}")
                res = subprocess.CompletedProcess(cmd, rc, "", "")
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
            if check:
                die(f"команда не найдена: {cmd[0]}")
            res = subprocess.CompletedProcess(cmd, 127, "", f"команда не найдена: {cmd[0]}")
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
        self.apt_update()
        self.run(
            ["apt-get", "install", "-y", "-qq", "--no-install-recommends", *packages],
            timeout=1800,
        )

    def pg_bin(self, name: str) -> str:
        """Путь к настоящему бинарю PostgreSQL.

        /usr/bin/psql — это perl-обёртка pg_wrapper; в части окружений
        (в т.ч. на минимальных ВМ) запуск через sudo -u postgres падает с
        «Permission denied». Ходим прямо в /usr/lib/postgresql/<ver>/bin.
        """
        direct = Path(f"/usr/lib/postgresql/{PG_VERSION}/bin/{name}")
        return str(direct) if direct.is_file() else name

    def psql(self, sql: str, db: str = "postgres", check: bool = True) -> subprocess.CompletedProcess:
        return self.run(
            ["sudo", "-u", "postgres", self.pg_bin("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db],
            input_text=sql,
            check=check,
        )

    def psql_scalar(self, sql: str, db: str = "postgres", check: bool = True) -> str:
        res = self.run(
            ["sudo", "-u", "postgres", self.pg_bin("psql"), "-X", "-tA", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", sql],
            check=check,
        )
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

    def write_file(self, path: Path, content, mode: int = 0o644, owner: str | None = None) -> bool:
        """Атомарно пишет файл, если содержимое изменилось. True = записан."""
        if self.check_mode:
            return False
        blob = content.encode() if isinstance(content, str) else content
        if path.is_file():
            try:
                if path.read_bytes() == blob:
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
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
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
        return self.write_file(dst, src.read_bytes(), mode=mode, owner=owner)

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
        # GITHUB_KEY — единственный секрет, который нужен ДО создания .env (шаг fetch:
        # реестр file_id из hermes-state). Если целимся в другой каталог стека,
        # добираем его из штатного .env.
        if not self.env.get("GITHUB_KEY"):
            fallback = parse_env(DEFAULT_INFRA_DIR / "ai" / ".env")
            if fallback.get("GITHUB_KEY"):
                self.env["GITHUB_KEY"] = fallback["GITHUB_KEY"]
            elif os.environ.get("GITHUB_KEY"):
                self.env["GITHUB_KEY"] = os.environ["GITHUB_KEY"]
            elif os.environ.get("GITHUB_TOKEN"):
                # bootstrap.sh отдаёт токен как GITHUB_TOKEN — тем же ключом
                # установщик ходит в приватный hermes-state за реестром file_id.
                self.env["GITHUB_KEY"] = os.environ["GITHUB_TOKEN"]
        # Маска из install.json не должна возвращаться в .env: токен живёт только в .env.
        tg = self.cfg.get("telegram") or {}
        for key in ("bot_token", "file_bot_token"):
            if tg.get(key) == MASKED_TOKEN:
                tg[key] = ""
        for item in getattr(self.args, "set", []) or []:
            key, _, value = str(item).partition("=")
            if key:
                self.env[key.strip()] = value

    def save_config(self) -> None:
        if self.dry or self.check_mode:
            return
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Токены живут только в .env — в install.json их быть не должно.
        safe = json.loads(json.dumps(self.cfg))
        tg = safe.get("telegram") or {}
        for key in ("bot_token", "file_bot_token"):
            if tg.get(key):
                tg[key] = MASKED_TOKEN
        data = json.dumps(safe, ensure_ascii=False, indent=2) + "\n"
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

    def confirm(self, label: str, default: bool = False) -> bool:
        """Да/нет с дефолтом; при --yes и без tty вопросы не задаются."""
        mark = "[Y/n]" if default else "[y/N]"
        try:
            ans = input(f"\n  {C_BOLD}{label}{C_RESET} {C_DIM}{mark}{C_RESET}: ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        return ans in ("y", "yes", "д", "да")

    def tty_read(self, label: str, secret: bool = False) -> str:
        """Прочитать строку с терминала, даже если stdin занят (`curl … | bash`)."""
        try:
            fd = os.open("/dev/tty", os.O_RDWR)
        except OSError:
            return ""
        attrs = None
        try:
            os.write(fd, f"  {label}: ".encode())
            if secret:
                import termios
                attrs = termios.tcgetattr(fd)
                quiet = [*attrs]
                quiet[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
            buf = b""
            while True:
                ch = os.read(fd, 1)
                if not ch or ch in (b"\n", b"\r"):
                    break
                buf += ch
            return buf.decode(errors="replace").strip()
        except OSError:
            return ""
        finally:
            if attrs is not None:
                import termios
                termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
                os.write(fd, b"\n")
            os.close(fd)

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
        tty_ok = Path("/dev/tty").exists()
        if self.args.yes or (not sys.stdin.isatty() and not tty_ok):
            if required and not cur:
                die(f"нет значения для «{label}». Запустите без --yes и введите его.")
            store[key] = cur
            return cur
        if hint:
            say(f"    {C_DIM}{hint}{C_RESET}")
        shown = mask(cur) if secret and cur else cur
        while True:
            suffix = f" [{shown}]" if shown else ""
            try:
                if sys.stdin.isatty():
                    if secret:
                        val = getpass.getpass(f"  {label}{suffix}: ")
                    else:
                        val = input(f"  {label}{suffix}: ")
                else:
                    val = self.tty_read(f"{label}{suffix}", secret=secret)
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

        say(f"  {C_DIM}Enter — оставить значение в скобках. Секреты вводятся скрыто.{C_RESET}")

        cfg["ai_domain"] = self.prompt(
            cfg, "ai_domain", "Домен Open WebUI", default=dom.get("ai", "ai.example.com"), required=True,
            hint="A-запись этого домена должна указывать на сервер (для TLS).",
        )
        cfg["llm_domain"] = self.prompt(
            cfg, "llm_domain", "Домен LiteLLM", default=dom.get("llm", "llm.example.com"), required=True
        )
        cfg["extra_domain"] = self.prompt(
            cfg, "extra_domain", "Доп. домен на open-terminal (можно пусто)",
            default=cfg.get("extra_domain", ""),
            hint="Дополнительный домен тоже терминируется на этом сервере; A-запись должна указывать сюда.",
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
        self.prompt(
            self.env, "ENABLE_DIRECT_INTEGRATIONS", "Прямые интеграции WebUI (true/false)",
            default=self.env.get("ENABLE_DIRECT_INTEGRATIONS", "true"),
            hint="Песочница и терминал в WebUI требуют true.",
        )
        self.prompt(
            self.env, "OPEN_TERMINAL_SANDBOX_API_KEY", "Ключ API песочницы терминала",
            default=self.env.get("OPEN_TERMINAL_SANDBOX_API_KEY") or secrets.token_urlsafe(24), secret=True,
            hint="Пусто — сгенерировать новый.",
        )

        # Telegram — необязательный блок.
        nenv = parse_env(NOTIF_DIR / ".env")
        cfg["telegram"] = cfg.get("telegram") or {}
        tg = cfg["telegram"]
        if not self.args.yes and sys.stdin.isatty():
            say(f"  {C_DIM}Telegram-уведомления: пустой токен — блок пропускается.{C_RESET}")
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

        # ── источник дампов: Telegram-бот вместо ручного bundle ────────────────
        bot = cfg.setdefault("bot_restore", {})
        if self.args.from_bot:
            self.bot_restore = True
        elif not self.args.yes and sys.stdin.isatty():
            self.bot_restore = self.confirm(
                "Тянуть последний бэкап БД из Telegram-бота?",
                default=bool(bot.get("enabled")),
            )
        bot["enabled"] = self.bot_restore
        if self.bot_restore:
            if not self.tg_token:
                nenv_tokens = parse_env(NOTIF_DIR / ".env")
                self.tg_token = self.prompt(
                    {}, "file_bot_token", "Токен файл-бота (FILE_BOT_TOKEN)",
                    default=nenv_tokens.get("FILE_BOT_TOKEN", ""),
                    secret=True, required=True,
                    hint="Тот же бот, которому уходят дампы. В install.json токен не пишется.",
                )
            info(f"дампы будут взяты из бота, токен {mask(self.tg_token)}")

        # ── состояние Hermes из приватного репозитория ─────────────────────────
        if self.args.hermes is not False:
            if self.args.restore_hermes is not None:
                self.hermes_restore = bool(self.args.restore_hermes)
            elif self.args.yes:
                # --yes без явного флага не разворачивает состояние поверх живого агента.
                self.hermes_restore = bool(self.args.hermes)
            elif sys.stdin.isatty():
                self.hermes_restore = self.confirm(
                    "Развернуть config/skills/скрипты Hermes из приватного репозитория?",
                    default=True,
                )
            # состояние теперь тянется без токена (публичный hermes-state)

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
        if getattr(self.args, "skip_tls", False):
            # Без TLS домена нет: ссылки должны вести на живой адрес машины.
            host = ""
            res = self.run(["bash", "-c", "hostname -I 2>/dev/null | awk '{print $1}'"], check=False, quiet=True)
            host = (res.stdout or "").strip().split()[0] if (res.stdout or "").strip() else ""
            self.env["WEBUI_URL"] = f"http://{host}:8080" if host else "http://127.0.0.1:8080"
        else:
            self.env["WEBUI_URL"] = f"https://{cfg['ai_domain']}"

        self.save_config()
        domains = ", ".join(d for d in (cfg["ai_domain"], cfg["llm_domain"], cfg.get("extra_domain")) if d)
        ok(f"Домены: {domains}")
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
            ver = (self.run(["docker", "--version"]).stdout or "").strip()
            ok(f"Docker {ver.split()[-1].rstrip(',')}" if ver else "Docker уже установлен")
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
        cv = (res.stdout or "").strip()
        ok(f"compose {cv.split()[-1]}" if cv else "compose v2 доступен")

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
            # Сам /home/ai_service: если его создал mkdir -p, владельцем остаётся root,
            # и ai_service не может прочитать свои же скрипты (systemd 203/EXEC).
            (AI_HOME, 0o755),
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

        # .env, распакованные из архива до создания пользователя.
        for target, inside_root in getattr(self, "secret_paths", []):
            if self.dry or not target.is_file():
                continue
            try:
                shutil.chown(target, "root", "root" if inside_root else AI_USER)
            except (KeyError, LookupError, PermissionError) as exc:
                warn(f"{target}: не удалось выставить владельца ({exc})")

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
        tg_override = self.cfg.setdefault("telegram", {})
        if self.args.bot_token:
            tg_override["bot_token"] = self.args.bot_token
        if self.args.file_bot_token:
            tg_override["file_bot_token"] = self.args.file_bot_token
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
        if tg.get("bot_token") and tg.get("bot_token") != MASKED_TOKEN:
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
                # Юнит ходит от ai_service: без группы сервис не прочитает .env
                # (ровно так 2026-10-24 отвалились оба бота).
                if not self.dry:
                    shutil.chown(nenv_path, "root", AI_USER)
                ok("notifications/.env записан (0640 root:ai_service)")
            else:
                ok("notifications/.env без изменений")
        else:
            warn("Telegram-токен не задан — мониторинг и алерты будут пропущены")

    # ── секреты из зашифрованного архива ────────────────────────────────────

    SECRETS_KEY_DEFAULT = Path("/root/.secrets/offsite.key")
    SECRETS_ITER = 300000

    def secret_set(self, registry: dict) -> dict | None:
        """Последний набор secrets_offsite в реестре file_id."""
        sets = [
            item
            for item in registry.get("sets", [])
            if isinstance(item, dict) and item.get("db") == "secrets_offsite" and item.get("parts")
        ]
        if not sets:
            return None
        return sorted(sets, key=lambda s: str(s.get("created_at") or ""))[-1]

    def secrets_blob(self) -> tuple[bytes, str]:
        """Зашифрованный архив: из файла или из файл-бота."""
        if self.args.secrets_archive:
            path = Path(self.args.secrets_archive).expanduser()
            if not path.is_file():
                die(f"архив не найден: {path}")
            return path.read_bytes(), path.name

        token = self.args.tg_token or parse_env(NOTIF_DIR / ".env").get("FILE_BOT_TOKEN") or ""
        if not token:
            die("нужен токен файл-бота: --tg-token или FILE_BOT_TOKEN в notifications/.env")
        registry = self.load_tg_registry() or {}
        if not registry:
            die("реестр file_id недоступен — укажите --tg-registry")
        entry = self.secret_set(registry)
        if not entry:
            die("в реестре нет набора secrets_offsite")
        parts = sorted(entry.get("parts") or [], key=lambda part: str(part.get("name")))
        info(f"архив секретов: {entry.get('dump') or '?'} ({len(parts)} частей)")
        blob = b"".join(self.tg_download(token, part) for part in parts)
        want = str(entry.get("sha256") or "")[:32]
        if want and hashlib.sha256(blob).hexdigest()[:32] != want:
            die("sha256 архива не совпал с реестром")
        return blob, str(entry.get("dump") or "secrets.enc")

    def override_hermes_token(self) -> None:
        """Подменить TELEGRAM_BOT_TOKEN в /root/.hermes/.env (прогон на тестовом боте)."""
        path = Path(os.environ.get("VPS_INFRA_HERMES_ENV", "/root/.hermes/.env"))
        if not path.is_file():
            die(f"нет {path} — переопределять токен некуда")
        token = str(self.args.hermes_token)
        lines = path.read_text().splitlines()
        out, seen = [], False
        for line in lines:
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                out.append(f"TELEGRAM_BOT_TOKEN={token}")
                seen = True
            else:
                out.append(line)
        if not seen:
            out.append(f"TELEGRAM_BOT_TOKEN={token}")
        os.chmod(path, 0o600)
        path.write_text("\n".join(out) + "\n")
        ok("токен Hermes переопределён (тестовый бот)")

    def restore_secrets(self) -> None:
        """Расшифровать архив и разложить .env по своим местам (0640 root:ai_service)."""
        step("Секреты из архива")
        key = Path(self.args.secrets_key or self.SECRETS_KEY_DEFAULT).expanduser()
        if not key.is_file():
            die(f"нет файла с паролем: {key} (--secrets-key)")
        blob, name = self.secrets_blob()
        if self.dry:
            info(f"[dry-run] расшифровал бы {name} паролем {key} и разложил .env по путям")
            return
        work = Path(tempfile.mkdtemp(prefix="secrets-"))
        enc = work / "secrets.enc"
        enc.write_bytes(blob)
        tar_path = work / "secrets.tar.gz"
        res = subprocess.run(
            [
                "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
                "-iter", str(self.SECRETS_ITER), "-pass", f"file:{key}",
                "-in", str(enc), "-out", str(tar_path),
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            die(f"не расшифровалось (не тот пароль?): {(res.stderr or '').strip()[:200]}")
        listing = subprocess.run(["tar", "-tzf", str(tar_path)], capture_output=True, text=True)
        if listing.returncode != 0:
            die("расшифрованный файл не является tar.gz")
        names = [line for line in (listing.stdout or "").splitlines() if line.strip()]
        root = Path(os.environ.get("VPS_INFRA_SECRETS_ROOT", "/"))
        self.run(["tar", "-xzf", str(tar_path), "-C", str(root)], timeout=120)

        placed: list[str] = []
        self.secret_paths: list[tuple[Path, bool]] = []
        for rel in names:
            target = root / rel.lstrip("./")
            if not target.is_file():
                continue
            placed.append(str(target))
            if target.name.startswith(".env"):
                inside_root = str(target).startswith(str(root / "root"))
                os.chmod(target, 0o600 if inside_root else 0o640)
                self.secret_paths.append((target, inside_root))
                try:
                    shutil.chown(target, "root", "root" if inside_root else AI_USER)
                except (KeyError, LookupError, PermissionError):
                    # пользователя ai_service ещё нет — дожмём после шага users
                    pass
        if not placed:
            die("в архиве не нашлось файлов .env")
        ok(f"разложено файлов: {len(placed)}")
        for path in placed:
            info(f"· {path}")

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

    # ── дампы из Telegram-бота ───────────────────────────────────────────────

    def http_get(self, url: str, headers: dict | None = None, timeout: int = 120) -> bytes:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def github_headers(self) -> dict:
        key = self.env.get("GITHUB_KEY", "")
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}", "Accept": "application/vnd.github.raw"}

    def load_tg_registry(self) -> dict | None:
        """Реестр file_id: явный --tg-registry → локальный файл → приватный hermes-state."""
        src = self.args.tg_registry
        if src:
            if src.startswith(("http://", "https://")):
                try:
                    raw = self.http_get(src, headers=self.github_headers())
                except (urllib.error.URLError, OSError) as exc:
                    die(f"реестр недоступен: {src}: {exc}")
            else:
                path = Path(src).expanduser()
                if not path.is_file():
                    die(f"реестр не найден: {path}")
                raw = path.read_bytes()
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                die(f"реестр не JSON: {src}: {exc}")

        local = self.dump_dir / ".tg-registry.json"
        if local.is_file():
            try:
                info(f"реестр file_id найден локально: {local}")
                return json.loads(local.read_text())
            except (OSError, json.JSONDecodeError):
                warn(f"локальный реестр повреждён: {local}")

        # публичный репозиторий: raw отдаёт файл без токена и без лимитов API
        raw_url = (
            f"https://raw.githubusercontent.com/{HERMES_STATE_REPO}/{HERMES_STATE_BRANCH}"
            "/host/backups/tg-registry.json"
        )
        try:
            return json.loads(self.http_get(raw_url))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                warn(f"реестр из raw.githubusercontent: HTTP {exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            warn(f"реестр из raw.githubusercontent: {exc}")

        url = (
            f"https://api.github.com/repos/{HERMES_STATE_REPO}/contents/"
            f"host/backups/tg-registry.json?ref={HERMES_STATE_BRANCH}"
        )
        try:
            raw = self.http_get(url, headers=self.github_headers())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                info(f"в {HERMES_STATE_REPO} ещё нет host/backups/tg-registry.json")
            else:
                warn(f"реестр из GitHub: HTTP {exc.code}")
            return None
        except (urllib.error.URLError, OSError) as exc:
            warn(f"реестр из GitHub: {exc}")
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            warn("реестр из GitHub не JSON")
            return None

    def latest_registry_sets(self, registry: dict) -> list[dict]:
        """Последний набор на каждую базу (webui_db, litellm_db) и архив файлов WebUI."""
        sets = [
            item for item in registry.get("sets", [])
            if isinstance(item, dict) and item.get("dump") and item.get("parts")
        ]
        best: dict[str, dict] = {}
        for entry in sorted(sets, key=lambda s: str(s.get("created_at") or "")):
            db = str(entry.get("db") or "")
            if db in RESTORE_DBS:
                best[db] = entry
        for db, entry in best.items():
            if not entry.get("complete", True):
                warn(f"{db}: последний набор {entry.get('dump')} помечен как неполный")
        return [best[db] for db in RESTORE_DBS if db in best]

    def sets_from_updates(self, token: str) -> list[dict]:
        """Fallback: файлы, которые владелец переслал боту (реестра нет/недоступен)."""
        url = (
            f"{TG_API}/bot{token}/getUpdates?limit=100"
            "&allowed_updates=%5B%22message%22%2C%22channel_post%22%5D"
        )
        try:
            data = json.loads(self.http_get(url))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            warn(f"getUpdates: {exc}")
            return []
        if not data.get("ok"):
            warn(f"getUpdates: {data.get('description')}")
            return []
        grouped: dict[str, dict] = {}
        for upd in data.get("result", []):
            msg = upd.get("message") or upd.get("channel_post") or {}
            doc = msg.get("document") or {}
            name = str(doc.get("file_name") or "")
            match = re.match(r"^(?P<db>.+?)_(?P<ts>\d{8}_\d{6})\.dump(?:\.part_(?P<part>\d+))?$", name)
            if not match or not doc.get("file_id"):
                continue
            db, ts = match.group("db"), match.group("ts")
            key = f"{db}|{ts}"
            entry = grouped.setdefault(
                key,
                {
                    "db": db,
                    "dump": f"{db}_{ts}.dump",
                    "complete": False,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(ts, "%Y%m%d_%H%M%S")),
                    "parts": [],
                },
            )
            entry["parts"].append(
                {
                    "name": name,
                    "size": int(doc.get("file_size") or 0),
                    "file_id": doc["file_id"],
                    "message_id": msg.get("message_id"),
                }
            )
        best: dict[str, dict] = {}
        for entry in grouped.values():
            entry["parts"].sort(key=lambda part: part["name"])
            entry["size"] = sum(int(part.get("size") or 0) for part in entry["parts"])
            db = entry["db"]
            if db in ("webui_db", "litellm_db") and (
                db not in best or entry["created_at"] > best[db]["created_at"]
            ):
                best[db] = entry
        if best:
            info("из getUpdates: " + ", ".join(f"{db} ← {entry['dump']}" for db, entry in best.items()))
        return [best[db] for db in ("webui_db", "litellm_db") if db in best]

    def tg_download(self, token: str, part: dict) -> bytes:
        name = str(part.get("name") or "?")
        file_id = str(part.get("file_id") or "")
        if not file_id:
            die(f"{name}: в реестре нет file_id")
        expected = int(part.get("size") or 0)
        if expected > TG_DOWNLOAD_LIMIT:
            die(
                f"{name}: {expected / 1024**2:.1f} MB — Bot API отдаёт не больше 20 MB. "
                "Нужны части ≤18 MB (TG_DOC_LIMIT в notifications/monitor.py) — "
                "дождитесь нового бэкапа или возьмите --bundle"
            )
        try:
            meta = json.loads(self.http_get(f"{TG_API}/bot{token}/getFile?file_id={file_id}"))
        except urllib.error.HTTPError as exc:
            die(f"getFile {name}: HTTP {exc.code} (file_id устарел?)")
        if not meta.get("ok"):
            die(f"getFile {name}: {meta.get('description')}")
        remote = meta.get("result") or {}
        if int(remote.get("file_size") or 0) > TG_DOWNLOAD_LIMIT:
            die(f"{name}: Telegram отдаёт {remote.get('file_size')} Б — больше лимита 20 MB")
        if not remote.get("file_path"):
            die(f"{name}: Telegram не вернул file_path")
        blob = self.http_get(f"{TG_API}/file/bot{token}/{remote['file_path']}", timeout=600)
        if expected and len(blob) != expected:
            die(f"{name}: скачано {len(blob)} Б вместо {expected} Б")
        want = part.get("sha256")
        digest = hashlib.sha256(blob).hexdigest()
        if want and digest != want:
            die(f"{name}: sha256 {digest[:12]} ≠ реестрового {str(want)[:12]}")
        info(f"{name}: {len(blob) / 1024**2:.1f} MB ✓")
        return blob

    def fetch_backup_set(self, token: str, entry: dict) -> None:
        dump_name = str(entry.get("dump") or "")
        parts = sorted(entry.get("parts") or [], key=lambda part: str(part.get("name")))
        if not dump_name or not parts:
            warn("пустой набор в источнике — пропускаю")
            return
        expected_size = int(entry.get("size") or 0)
        target = self.dump_dir / dump_name
        if target.is_file() and (not expected_size or target.stat().st_size == expected_size):
            ok(f"{dump_name}: уже на месте — скачивание не нужно")
            return
        if self.dry:
            info(f"[dry-run] скачал бы {dump_name}: {len(parts)} частей, {expected_size / 1024**2:.1f} MB")
            return
        self.ensure_dir(self.dump_dir, 0o755, AI_USER)
        work = STATE_DIR / "tg-restore"
        self.ensure_dir(work, 0o700)
        joined = work / dump_name
        with joined.open("wb") as fh:
            for part in parts:
                fh.write(self.tg_download(token, part))

        size = joined.stat().st_size
        if expected_size and size != expected_size:
            die(f"{dump_name}: склейка {size} Б ≠ ожидаемых {expected_size} Б")
        digest = hashlib.sha256()
        with joined.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        want = entry.get("sha256")
        if want and digest.hexdigest() != want:
            die(f"{dump_name}: sha256 склейки не совпал — файл повреждён при передаче")

        tmp_target = self.dump_dir / f".{dump_name}.tmp"
        shutil.copyfile(joined, tmp_target)
        os.replace(tmp_target, target)
        stamp = entry.get("created_at")
        if stamp:
            try:
                epoch = calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
                os.utime(target, (epoch, epoch))
            except (ValueError, OverflowError):
                pass
        try:
            shutil.chown(target, AI_USER, AI_USER)
        except (KeyError, PermissionError):
            pass
        ok(f"{dump_name}: {size / 1024**2:.1f} MB, sha256 ok → {target}")

    def step_fetch(self) -> None:
        step("Дампы из Telegram-бота")
        if not self.bot_restore:
            info("выключено (--from-bot чтобы включить)")
            return
        token = self.tg_token or parse_env(NOTIF_DIR / ".env").get("FILE_BOT_TOKEN", "")
        if not token:
            die("нет токена файл-бота: задайте --tg-token или notifications/.env")
        if token == MASKED_TOKEN or not re.match(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$", token):
            die(
                "токен не похож на токен Telegram-бота (ожидается «123456:AA…»). "
                f"Проверьте FILE_BOT_TOKEN в {NOTIF_DIR / '.env'} — возможно, там заглушка"
            )

        registry = self.load_tg_registry()
        entries = self.latest_registry_sets(registry) if registry else []
        if not entries:
            warn("реестр file_id недоступен — смотрю getUpdates (пересланные боту файлы)")
            entries = self.sets_from_updates(token)
        if not entries:
            die(
                "в боте не нашлось дампов: перешлите файлы бэкапа боту и повторите, "
                "либо укажите --bundle / локальные дампы"
            )
        for entry in entries:
            self.fetch_backup_set(token, entry)

        self.dump_from_bot = True
        self.cfg["dump_source"] = "telegram"
        self.save_config()

    PGDG_KEY_URL = "https://www.postgresql.org/media/keys/ACCC4CF8.asc"
    PGDG_FPR = "ACCC4CF8"

    def check_fake_dns(self) -> None:
        """Предупредить, если DNS отдаёт fake-IP (VPN/прокси-клиент на хосте).

        Адреса 198.18.0.0/15 зарезервированы под тесты: их выдают Clash,
        sing-box, Hiddify и подобные. Через такой прокси apt получает
        закэшированный индекс, не совпадающий по размеру с Release, и
        репозиторий «не подписан»/«не скачался» на ровном месте.
        """
        res = self.run(["getent", "ahostsv4", "apt.postgresql.org"], check=False)
        addrs = [word for word in (res.stdout or "").split() if word.count(".") == 3]
        fake = [a for a in addrs if a.startswith("198.18.") or a.startswith("198.19.")]
        if fake:
            warn(f"DNS отдаёт fake-IP {fake[0]} — похоже, на хосте включён VPN/прокси")
            warn("apt через такой прокси получает битый индекс: отключите туннель или задайте DNS 1.1.1.1 в ВМ")

    def install_pgdg_key(self) -> None:
        """Скачать ключ pgdg, проверить его и положить бинарным keyring'ом.

        Armored-файл в signed-by apt читает не всегда: если файл не разобран,
        apt пишет NO_PUBKEY и репозиторий считается неподписанным. Поэтому
        ключ приводим к бинарному виду и проверяем, что отпечаток совпал.
        """
        keyring = Path("/usr/share/keyrings/pgdg.gpg")
        asc = Path("/usr/share/keyrings/pgdg.asc")
        self.ensure_dir(keyring.parent, 0o755)
        res = self.run(["curl", "-fsSL", "--retry", "3", "-o", str(asc), self.PGDG_KEY_URL], check=False)
        if res.returncode != 0 or not asc.is_file() or asc.stat().st_size < 500:
            die(f"не скачался ключ pgdg: {self.PGDG_KEY_URL} (rc={res.returncode})")
        head = asc.read_text(errors="replace")[:80]
        if "BEGIN PGP PUBLIC KEY BLOCK" not in head:
            die("скачанный ключ pgdg не похож на PGP-блок (прокси или заглушка?)")
        if shutil.which("gpg"):
            res = self.run(["gpg", "--batch", "--yes", "--dearmor", "--output", str(keyring), str(asc)], check=False)
            if res.returncode != 0:
                die(f"gpg --dearmor не смог разобрать ключ pgdg: {(res.stderr or '').strip()[:150]}")
        else:
            shutil.copyfile(asc, keyring)
        os.chmod(keyring, 0o644)
        shown = self.run(["gpg", "--show-keys", "--with-colons", str(keyring)], check=False)
        if self.PGDG_FPR not in (shown.stdout or ""):
            die(f"в keyring нет ожидаемого отпечатка {self.PGDG_FPR}")
        ok("ключ pgdg проверен и установлен")

    PGDG_LIST = Path("/etc/apt/sources.list.d/pgdg.list")

    def _apt_blob(self, res: subprocess.CompletedProcess) -> str:
        return ((res.stdout or "") + (res.stderr or "")).strip()

    def _neutralize_pgdg(self) -> None:
        """Временно убрать pgdg из apt: без него не поставить gnupg и починить ключ."""
        if not self.PGDG_LIST.exists():
            return
        backup = self.PGDG_LIST.with_suffix(".list.bak")
        if not backup.exists():
            shutil.copyfile(self.PGDG_LIST, backup)
        if not self.dry:
            self.PGDG_LIST.write_text(
                "# временно отключено установщиком: ключ pgdg переустанавливается\n"
            )

    def _codename(self) -> str:
        """Кодовое имя дистрибутива без зависимости от пакета lsb-release."""
        rel = Path("/etc/os-release")
        if rel.is_file():
            for line in rel.read_text(errors="replace").splitlines():
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"') or "noble"
        if shutil.which("lsb_release"):
            res = self.run(["lsb_release", "-cs"], check=False)
            if res.returncode == 0 and (res.stdout or "").strip():
                return res.stdout.strip()
        return "noble"

    def _write_pgdg_list(self) -> None:
        # http, а не https: проверку подписи делает GPG, а Fastly-кэш https
        # у части провайдеров отдаёт устаревший InRelease и валит apt.
        codename = self._codename()
        if not self.dry:
            self.PGDG_LIST.write_text(
                "deb [signed-by=/usr/share/keyrings/pgdg.gpg] "
                f"http://apt.postgresql.org/pub/repos/apt {codename}-pgdg main\n"
            )

    APT_TRANSIENT = (
        "unexpected size",
        "mirror sync",
        "hash sum mismatch",
        "failed to fetch",
        "some index files failed",
        "temporary failure",
        "could not connect",
        "connection failed",
        "503",
        "502",
    )

    def _purge_pgdg_lists(self) -> int:
        """Снести кэш индексов pgdg: старый InRelease в /var/lib/apt/lists
        держит ожидаемый размер файла, и apt вечно ругается на unexpected size."""
        removed = 0
        for path in Path("/var/lib/apt/lists").glob("*pgdg*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _apt_update_once(self, no_cache: bool = False) -> subprocess.CompletedProcess:
        cmd = ["apt-get", "update", "-qq", "-o", "Acquire::Retries=5", "-o", "Acquire::http::Timeout=30"]
        if no_cache:
            # обойти промежуточный кэш (VPN/прокси с fake-IP), который отдаёт старый индекс
            cmd += ["-o", "Acquire::http::No-Cache=true", "-o", "Acquire::http::Pipeline-Depth=0"]
        return self.run(cmd, check=False)

    def apt_update(self) -> None:
        """apt-get update: чинит подпись pgdg и переживает рассинхрон зеркал.

        Два known-случая, из-за которых раньше падала вся установка:
        1) остался старый pgdg.list без ключа → apt валит update с NO_PUBKEY;
        2) зеркало apt.postgresql.org в момент обновления отдаёт файл с другим
           размером/хэшем («Mirror sync in progress») → apt отдаёт код 100.
        Второе лечится паузой и повтором, а не падением установщика.
        """
        repaired = False
        purged = False
        blob = ""
        for attempt in range(4):
            res = self._apt_update_once(no_cache=attempt == 3)
            if res.returncode == 0:
                if attempt:
                    ok("индексы apt обновлены после повтора")
                return
            blob = self._apt_blob(res)
            low = blob.lower()

            if ("no_pubkey" in low or "not signed" in low) and not repaired:
                repaired = True
                warn("apt не принимает подпись pgdg — переустанавливаю ключ")
                if not (shutil.which("gpg") and shutil.which("curl")):
                    self._neutralize_pgdg()
                    res = self._apt_update_once()
                    if res.returncode != 0:
                        self.log(self._apt_blob(res)[-800:])
                        die("apt-get update не прошёл даже без pgdg")
                    self.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends",
                              "gnupg", "curl", "ca-certificates"], timeout=900)
                self.install_pgdg_key()
                self._write_pgdg_list()
                continue

            if "unexpected size" in low and not purged:
                purged = True
                gone = self._purge_pgdg_lists()
                warn(f"старый индекс в кэше apt — чищу ({gone} файл.) и качаю заново")
                continue

            if any(marker in low for marker in self.APT_TRANSIENT) and attempt < 3:
                wait = (5, 15, 30)[attempt]
                warn(f"зеркало apt отдало рассинхрон — повтор {attempt + 1}/3 через {wait} c")
                time.sleep(wait)
                continue
            break

        self.log(blob[-1200:])
        tail = blob.splitlines()[-1][:180] if blob else f"rc={res.returncode}"
        die(f"apt-get update не прошёл: {tail}")

    def step_postgres(self) -> None:
        step(f"PostgreSQL {PG_VERSION} + pgvector")
        cluster = Path(f"/etc/postgresql/{PG_VERSION}/main")
        need = False
        res = None
        if shutil.which("psql"):
            res = self.run(["psql", "--version"], check=False)
        if res is None or res.returncode != 0 or f" {PG_VERSION}." not in (res.stdout or ""):
            need = True
        if not cluster.is_dir():
            need = True

        if need:
            info(f"ставлю PostgreSQL {PG_VERSION} из pgdg")
            self.apt_install(["curl", "ca-certificates", "gnupg", "lsb-release"])
            self.check_fake_dns()
            self.install_pgdg_key()
            self._write_pgdg_list()
            self.apt_update()
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
        """Дождаться готовности PostgreSQL и внятно объяснить, если не поднялся."""
        if self.dry:
            return
        last = ""
        for _ in range(60):
            res = self.run(
                ["sudo", "-u", "postgres", self.pg_bin("psql"), "-tAc", "SELECT 1"], check=False, quiet=True
            )
            if res.returncode == 0:
                return
            last = ((res.stderr or "") + (res.stdout or "")).strip().splitlines()
            last = last[-1][:200] if last else f"rc={res.returncode}"
            time.sleep(1)
        st = self.run(["systemctl", "status", "postgresql", "--no-pager", "-l"], check=False, quiet=True)
        clusters = self.run(["pg_lsclusters"], check=False, quiet=True)
        journal = self.run(["journalctl", "-u", "postgresql", "-n", "20", "--no-pager"], check=False, quiet=True)
        for blob in (clusters.stdout, st.stdout, journal.stdout):
            if blob:
                self.log(blob[:2000])
        die(f"PostgreSQL не отвечает 60 c: {last}")

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

        # Образы, у которых в compose есть build:, нельзя тянуть из реестра —
        # их собирают на месте (WebUI-брендинг, песочница). Иначе на чистой
        # машине установщик падает на docker pull несуществующего :local-тега.
        builds = self.compose_build_targets()
        for img, service in builds.items():
            if self.image_exists(img):
                ok(f"{img} уже собран")
                continue
            info(f"собираю {img} ({service}, может занять несколько минут)")
            self.compose("build", service, stream=True, timeout=3600)
            ok(f"{img} собран")

        # Остальные образы тянем только если их нет: :latest-теги не должны
        # молча обновляться при повторном запуске установщика.
        missing = False
        for img in self.compose_images():
            if img in builds:
                continue
            if not self.image_exists(img):
                info(f"тяну {img}")
                self.run(["docker", "pull", img], stream=True, timeout=3600)
                missing = True
        ok("образы на месте" if not missing else "образы загружены")

    def compose_build_targets(self) -> dict[str, str]:
        """Образы, которые собираются локально: {image: имя сервиса}."""
        res = self.compose("config", "--format", "json", quiet=True)
        try:
            data = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            return {}
        targets: dict[str, str] = {}
        for name, svc in (data.get("services") or {}).items():
            if isinstance(svc, dict) and svc.get("build") and svc.get("image"):
                targets[str(svc["image"])] = name
        return targets

    def ensure_roles_and_databases(self) -> None:
        """Роли webui/litellm и их базы + pgvector. Идемпотентно."""
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
        step("Роли, базы и миграции LiteLLM")
        # Базы и роли создаём до старта LiteLLM: иначе контейнер подключается
        # к несуществующей базе, а проверка схемы падает с FATAL.
        self.ensure_roles_and_databases()
        self.compose("up", "-d", "litellm-redis", "litellm", stream=True, timeout=1200)
        if self.dry:
            return
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
        # Дамп, только что скачанный из бота, — это осознанный выбор источника, а не
        # «залежавшийся файл»: проверку свежести к нему не применяем.
        from_bot = self.dump_from_bot
        dbs = [("litellm_db", "litellm"), ("webui_db", "webui")]
        # Схема после миграций уже создана, поэтому «есть таблицы» ≠ «есть данные»:
        # пустую базу с мигрированной схемой нужно восстанавливать, иначе дамп молча пропускается.
        probes = {"litellm_db": '"LiteLLM_VerificationToken"', "webui_db": '"user"'}
        todo = []
        for db, owner in dbs:
            tables = self.psql_scalar("SELECT count(*) FROM pg_tables WHERE schemaname='public'", db=db)
            dump = self.find_dump(db)
            if not dump:
                info(f"{db}: дамп не найден — пропускаю")
                continue
            age_h = (time.time() - dump.stat().st_mtime) / 3600
            if age_h > STALE_HOURS and not self.args.force_stale and not bundle and not from_bot:
                warn(f"{db}: дамп {dump.name} старше {STALE_HOURS} ч ({age_h:.0f} ч) — пропускаю (--force-stale чтобы применить)")
                continue
            rows = (self.psql_scalar(f"SELECT count(*) FROM {probes[db]}", db=db, check=False)
                    if tables not in ("0", "") else "0")
            has_data = rows not in ("0", "", None)
            if has_data and not restore_all:
                info(f"{db}: в базе уже {tables} таблиц и {rows} строк данных — восстановление пропущено "
                     f"(--restore чтобы перезаписать)")
                continue
            if tables not in ("0", "") and not has_data:
                info(f"{db}: схема создана миграциями, данных нет — восстанавливаю дамп")
            todo.append((db, owner, dump))

        if not todo:
            ok("восстанавливать нечего")
            return

        for db, owner, dump in todo:
            info(f"{db} ← {dump.name} ({dump.stat().st_size / 1024**2:.1f} MB)")
            log = STATE_DIR / f"restore-{db}.log"
            if self.dry:
                proc = subprocess.CompletedProcess([], 0)
            else:
                with dump.open("rb") as dump_fh, log.open("w") as lf:
                    proc = subprocess.run(
                        ["sudo", "-u", "postgres", "pg_restore", f"--dbname={db}", "--clean", "--if-exists",
                         "--no-owner", "--no-privileges", "--single-transaction", "--verbose"],
                        stdin=dump_fh, stdout=lf, stderr=subprocess.STDOUT,
                    )
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

    def step_data(self) -> None:
        """Распаковать архив пользовательских файлов WebUI, скачанный из бота."""
        step("Файлы Open WebUI")
        archives = sorted(
            self.dump_dir.glob("webui_data_files_*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not archives:
            info("архива webui_data нет — пропускаю")
            return
        archive = archives[0]
        if WEBUI_DATA.is_dir() and any(WEBUI_DATA.iterdir()) and not self.args.restore:
            info("webui_data уже не пуст — пропускаю (--restore чтобы перезаписать)")
            return
        info(f"{archive.name} ({archive.stat().st_size / 1024**2:.0f} MB) → {WEBUI_DATA}")
        if self.dry:
            info("[dry-run] распаковка пропущена")
            return
        self.ensure_dir(WEBUI_DATA, 0o755, AI_USER)
        try:
            with tarfile.open(archive) as tar:
                try:
                    tar.extractall(WEBUI_DATA, filter="data")
                except TypeError:  # Python < 3.12
                    tar.extractall(WEBUI_DATA)
        except (tarfile.TarError, OSError) as exc:
            die(f"не удалось распаковать {archive.name}: {exc}")
        self.run(["chown", "-R", f"{AI_USER}:{AI_USER}", str(WEBUI_DATA)])
        ok(f"файлы WebUI восстановлены из {archive.name}")
        self.changed.append(str(archive))

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
        if self.dry:
            return
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

        tg = self.cfg.get("telegram") or {}
        timers = list(CORE_TIMERS)
        if not tg.get("bot_token"):
            timers.remove("postgres-backup-notify.timer")
        units = timers + CORE_SERVICES
        # Таймеру нужен его .service в /etc/systemd/system, иначе systemd
        # отказывается стартовать таймер («unit to trigger not loaded»).
        for t in timers:
            svc = t.replace(".timer", ".service")
            if (ASSETS / "host" / "systemd" / svc).is_file() and svc not in units:
                units.append(svc)
        enable = list(timers) + list(CORE_SERVICES)
        if tg.get("bot_token"):
            units += NOTIFY_SERVICES
            enable += NOTIFY_SERVICES
        self.install_units(units, enable)
        ok(f"юниты включены ({len(enable)} шт.)")

        # Разовый прогон бэкапа, чтобы проверить, что он работает. Если свежий
        # дамп уже есть (< 1 ч), не гоняем pg_dump повторно на каждом запуске.
        if not self.dry and (self.compose_dir / ".env").is_file():
            dumps = list(self.dump_dir.glob("*.dump"))
            fresh = max(dumps, key=lambda p: p.stat().st_mtime, default=None)
            age = (time.time() - fresh.stat().st_mtime) if fresh else 1e9
            if age < 3600:
                info("свежий бэкап БД уже есть — пропускаю")
            else:
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
        server_limits = ""
        api_location = ""
        if port == 4000:
            # LiteLLM: лимит на весь server, тут нет статики.
            server_limits = (
                "    # anti-flood: 10 r/s на IP, всплеск 40, сверх — 429\n"
                "    limit_req zone=llm_api burst=40 nodelay;\n"
                "    limit_req_status 429;\n"
                "    limit_conn conn_per_ip 20;\n\n"
            )
        elif port == 8080:
            # WebUI: лимит только на /api/ — иначе пачка чанков при F5 даёт 429.
            api_location = f"""    # anti-flood только на API: статика (/assets, /_app) идёт без лимита
    location /api/ {{
        limit_req zone=webui_api burst=30 nodelay;
        limit_req_status 429;
        limit_conn conn_per_ip 30;

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

"""
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

{server_limits}{api_location}    location / {{
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

    def step_beszel(self) -> None:
        step("Beszel (графики и пороги)")
        target = Path("/home/ai_service/beszel")
        self.ensure_dir(target, 0o755, AI_USER)
        self.sync_tree(ASSETS / "stack" / "beszel", target, AI_USER, skip=(".env", "__pycache__", ".git"))
        # .env с TOKEN/KEY заполняется один раз после добавления системы в UI —
        # перезаписывать его нельзя, иначе агент потеряет регистрацию.
        env = target / ".env"
        if not env.exists() and not self.dry:
            env.write_text("TOKEN=\nKEY=\n")
            os.chmod(env, 0o600)
        if self.dry:
            info("[dry-run] docker compose up -d beszel (hub)")
            return
        self.run(["docker", "compose", "up", "-d", "beszel"], cwd=target, check=False, timeout=300)
        res = self.run(["curl", "-fsS", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}",
                        "http://127.0.0.1:8090/"], check=False)
        if (res.stdout or "").strip() == "200":
            ok("Beszel hub отвечает на 127.0.0.1:8090")
        else:
            warn("Beszel hub не отвечает — docker logs beszel")
        if not any(l.startswith("TOKEN=") and l.strip() != "TOKEN=" for l in env.read_text().splitlines()):
            info("агент не запущен: добавь систему в UI (host /beszel_socket/beszel.sock) и впиши TOKEN/KEY в .env")

    def step_hardening(self) -> None:
        step("fail2ban и anti-flood")
        if shutil.which("fail2ban-client") is None:
            self.apt_install(["fail2ban", "python3-systemd"])
            ok("fail2ban установлен")
        else:
            ok("fail2ban уже установлен")
        # jail.local перекрывает jail.conf и jail.d/defaults-debian.conf.
        self.write_file(Path("/etc/fail2ban/jail.local"), FAIL2BAN_JAIL, 0o644)
        res = self.run(["fail2ban-client", "-t"], check=False)
        if res.returncode != 0:
            warn("jail.local не проходит проверку — fail2ban оставлен как есть")
            return
        self.run(["systemctl", "enable", "--now", "fail2ban"], check=False)
        state = self.run(["systemctl", "is-active", "fail2ban"], check=False).stdout.strip()
        if state == "active":
            ok("fail2ban активен (sshd: maxretry=4/1ч, бан 1д+)")
        else:
            warn(f"fail2ban не активен (state={state or '?'}) — journalctl -u fail2ban")

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
        }
        extra = (self.cfg.get("extra_domain") or "").strip()
        if extra:
            sites[extra] = ("127.0.0.1", 8000)
        # Beszel hub (шаг beszel): mon.<base-domain> → 127.0.0.1:8090
        mon = (self.cfg.get("mon_domain") or "").strip()
        if not mon and "." in (self.cfg.get("ai_domain") or ""):
            mon = "mon." + self.cfg["ai_domain"].split(".", 1)[1]
        if mon:
            sites[mon] = ("127.0.0.1", 8090)
        avail = Path("/etc/nginx/sites-available")
        enabled = Path("/etc/nginx/sites-enabled")
        self.ensure_dir(avail, 0o755)
        self.ensure_dir(enabled, 0o755)
        # Зоны rate-limit обязаны существовать до nginx -t: vhost'ы на них ссылаются.
        confd = Path("/etc/nginx/conf.d")
        self.ensure_dir(confd, 0o755)
        self.write_file(confd / "zz-ratelimit.conf", RATE_LIMIT_CONF, 0o644)

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

    POSTCHECK_PROMPT = """Ты — дежурный администратор этого сервера. Только что завершилось автоматическое развёртывание стека.

Первым делом прочитай ранбук {report_path} и выполни его раздел 1 «Пост-проверка после развёртывания» — целиком, по шагам, которые там описаны. В файле точные команды, ожидаемые результаты и то, что считается отклонением; не заменяй его содержимое импровизацией и не выдумывай свои проверки вместо описанных.

Дальше — один короткий отчёт владельцу.

Только чтение: не меняй конфиги, ничего не перезапускай и не удаляй — тебе нужно собрать факты и отчитаться. Если ранбук требует изменения состояния, остановись и сформулируй, что именно нужно и почему.

Формат: не больше 25 строк, по-русски, без воды и без секретов. Сначала вердикт одной строкой («всё по топологии» или «есть отклонения»), затем только отклонения списком: что не так и что сделать. Если всё в порядке — перечисли одной строкой, что именно проверил. Не предлагай переустанавливать стек.
"""

    REPORT_FILES = (
        ("ai/.env", "OPEN WEBUI / LITELLM"),
        ("notifications/.env", "БОТЫ УВЕДОМЛЕНИЙ"),
        ("/root/.hermes/.env", "HERMES"),
    )

    def report_groups(self) -> list[tuple[str, list[str]]]:
        """Секреты, сгруппированные по файлам — как они лежат на диске."""
        out = []
        paths = {
            "ai/.env": self.compose_dir / ".env",
            "notifications/.env": NOTIF_DIR / ".env",
            "/root/.hermes/.env": Path("/root/.hermes/.env"),
        }
        for label, title in self.REPORT_FILES:
            path = paths[label]
            if not path.is_file():
                continue
            lines = [ln.strip() for ln in path.read_text(errors="replace").splitlines()]
            lines = [ln for ln in lines if ln and not ln.startswith("#")]
            if lines:
                out.append((f"{title}  ({label})", lines))
        cfg_env = self.cfg.get("telegram") or {}
        extra = [f"BOT_OWNER={cfg_env.get('owner', '')}"] if cfg_env.get("owner") else []
        if extra:
            out.append(("ПАРАМЕТРЫ УСТАНОВКИ", extra))
        return out

    def step_report(self) -> None:
        """Отчёт с секретами: зашифрованный файл + ключ отдельным сообщением."""
        step("Отчёт с секретами в Telegram")
        if getattr(self.args, "no_report", False):
            info("--no-report: отчёт не отправляю")
            return
        nenv = parse_env(NOTIF_DIR / ".env")
        token = nenv.get("BOT_TOKEN") or nenv.get("FILE_BOT_TOKEN") or ""
        owner = nenv.get("BOT_OWNER") or (self.cfg.get("telegram") or {}).get("owner") or ""
        if not token or not owner:
            warn("нет токена бота или chat_id владельца — отчёт пропущен")
            return
        groups = self.report_groups()
        if not groups:
            warn("нечего отправлять: .env файлы не найдены")
            return
        if self.dry:
            info(f"[dry-run] отправил бы {sum(len(v) for _, v in groups)} строк в чат {owner}")
            return

        stamp = time.strftime("%Y%m%d_%H%M%S")
        lines = [f"Установка {stamp} UTC", ""]
        for title, body in groups:
            lines.append(f"### {title}")
            lines.extend(body)
            lines.append("")
        payload = "\n".join(lines)

        key = secrets.token_urlsafe(32)
        enc_path = STATE_DIR / f"secrets-{stamp}.enc"
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
             "-salt", "-pass", f"pass:{key}", "-out", str(enc_path)],
            input=payload.encode(), capture_output=True,
        )
        if proc.returncode != 0:
            warn("не удалось зашифровать отчёт — секреты не отправляю")
            return
        os.chmod(enc_path, 0o600)

        api = f"https://api.telegram.org/bot{token}"
        send = self.run(
            ["curl", "-fsS", "--max-time", "120", "-F", f"chat_id={owner}",
             "-F", "caption=Секреты установки (зашифровано). Ключ — следующим сообщением.",
             "-F", f"document=@{enc_path}"],
            check=False, quiet=True,
        )
        if send.returncode != 0:
            warn("файл с секретами не отправился (проверьте токен/чат)")
            return
        ok("файл с секретами отправлен")

        instructions = (
            "Ключ к архиву секретов (сохраните и удалите это сообщение):\n"
            f"{key}\n\n"
            "Расшифровка:\n"
            f"openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in secrets-{stamp}.enc "
            "-out secrets.txt -pass pass:<ключ>"
        )
        self.run(["curl", "-fsS", "--max-time", "60", "-X", "POST",
                  f"{api}/sendMessage", "-d", f"chat_id={owner}",
                  "--data-urlencode", f"text={instructions}"], check=False, quiet=True)
        self.run(["curl", "-fsS", "--max-time", "60", "-X", "POST",
                  f"{api}/sendMessage", "-d", f"chat_id={owner}",
                  "--data-urlencode",
                  "text=Сообщение с ключом удалите из чата сразу после сохранения: "
                  "кто видел ключ и файл — прочитает секреты."], check=False, quiet=True)
        ok("ключ отправлен отдельным сообщением")

    def step_postcheck(self) -> None:
        """Одноразовый таймер: через N минут Hermes сам проверяет сервер и пишет в TG."""
        step("Пост-проверка Hermes (таймер)")
        if getattr(self.args, "no_postcheck", False):
            info("--no-postcheck: таймер не ставлю")
            return
        if not shutil.which("hermes") and not Path("/usr/local/bin/hermes").is_file():
            warn("Hermes не установлен — пост-проверку не ставлю")
            return

        delay = getattr(self.args, "postcheck_delay", "15min") or "15min"
        prompt_path = Path("/etc/hermes-postcheck.prompt")
        script_path = Path("/usr/local/bin/hermes-postcheck.sh")
        service = Path("/etc/systemd/system/hermes-postcheck.service")
        timer = Path("/etc/systemd/system/hermes-postcheck.timer")

        if self.dry:
            info(f"[dry-run] поставил бы таймер на +{delay}: hermes chat --oneshot → отчёт в Telegram")
            return

        self.write_file(prompt_path, self.POSTCHECK_PROMPT.replace("{report_path}", str(ROOT / "REPORT.md")), 0o600)
        self.write_file(script_path, """#!/bin/bash
# Одноразовая пост-проверка сервера агентом Hermes. Отчёт уходит владельцу в Telegram.
set -uo pipefail
LOG=/var/log/hermes-postcheck.log
REPORT=/var/lib/hermes-postcheck/report.md
mkdir -p "$(dirname "$REPORT")"
{
    echo "=== пост-проверка $(date -u +%FT%TZ) ==="
    /usr/local/bin/hermes chat --query-file /etc/hermes-postcheck.prompt --oneshot -Q --yolo 2>&1
} > "$REPORT"
/usr/local/bin/hermes send -t telegram -f "$REPORT" \
    -s "Пост-проверка после развёртывания" >>"$LOG" 2>&1
systemctl disable --now hermes-postcheck.timer >>"$LOG" 2>&1 || true
exit 0
""", 0o755)
        self.write_file(service, """[Unit]
Description=Hermes: пост-проверка сервера после развёртывания
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/hermes-postcheck.sh
TimeoutStartSec=1800
Nice=10
""", 0o644)
        self.write_file(timer, f"""[Unit]
Description=Одноразовый запуск пост-проверки Hermes через {delay} после развёртывания

[Timer]
OnActiveSec={delay}
AccuracySec=1min
Unit=hermes-postcheck.service

[Install]
WantedBy=timers.target
""", 0o644)
        self.run(["systemctl", "daemon-reload"], check=False)
        self.run(["systemctl", "enable", "hermes-postcheck.timer"], check=False)
        self.run(["systemctl", "restart", "hermes-postcheck.timer"], check=False)
        ok(f"пост-проверка запустится через {delay}, отчёт придёт в Telegram")

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
                say(f"  {C_RED}✗{C_RESET} {name}: не найден")
                healthy = False
                continue
            parts = (res.stdout or "").strip().split("|")
            state = parts[0]
            health = parts[1] if len(parts) > 1 else ""
            restarts = parts[2] if len(parts) > 2 else "?"
            good = state == "running" and health in ("healthy", "")
            mark = f"{C_GREEN}✓{C_RESET}" if good else f"{C_RED}✗{C_RESET}"
            say(f"  {mark} {name}: {state} {health} restarts={restarts}")
            healthy = healthy and good

        for db in ("webui_db", "litellm_db"):
            n = self.psql_scalar("SELECT count(*) FROM pg_tables WHERE schemaname='public'", db=db)
            say(f"  {C_DIM}·{C_RESET} {db}: {n or '?'} таблиц")

        checks = [
            ("litellm", "http://127.0.0.1:4000/health/liveliness"),
            ("open-webui", "http://127.0.0.1:8080/health"),
            ("searxng", "http://127.0.0.1:8081/healthz"),
        ]
        for name, url in checks:
            res = self.run(["curl", "-fsS", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", url], check=False)
            say(f"  {C_DIM}·{C_RESET} {name}: HTTP {res.stdout.strip() or 'ERR'}")
        # open-terminal не публикует порт на хост — проверяем изнутри контейнера.
        res = self.run(
            ["docker", "exec", "open-terminal", "curl", "-fsS", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}",
             "http://127.0.0.1:8000/health"],
            check=False,
        )
        say(f"  {C_DIM}·{C_RESET} open-terminal (в контейнере): HTTP {res.stdout.strip() or 'ERR'}")

        if not self.args.skip_tls:
            for domain in (self.cfg.get("ai_domain"), self.cfg.get("llm_domain"), self.cfg.get("extra_domain")):
                if not domain:
                    continue
                res = self.run(
                    ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-m", "10", f"https://{domain}"],
                    check=False,
                )
                code = res.stdout.strip() or "ERR"
                say(f"  {C_DIM}·{C_RESET} https://{domain}: {code}")

        active = self.run(["systemctl", "list-timers", "--no-legend", "--plain"], check=False).stdout or ""
        marks = [u for u in CORE_TIMERS if u in active]
        say(f"  {C_DIM}·{C_RESET} таймеров активно: {len(marks)}/{len(CORE_TIMERS)}")

        f2b = self.run(["systemctl", "is-active", "fail2ban"], check=False).stdout.strip()
        say(f"  {C_DIM}·{C_RESET} fail2ban: {f2b or 'ERR'}")
        limited = Path("/etc/nginx/conf.d/zz-ratelimit.conf").is_file()
        say(f"  {C_DIM}·{C_RESET} nginx rate-limit: {'да' if limited else 'нет'}")
        if f2b != "active" and not self.args.skip_tls:
            warn("fail2ban не активен — SSH без rate-limit защиты")

        if not healthy:
            self.failed.append("не все контейнеры здоровы")
        if healthy:
            ok("стек здоров")
        else:
            warn("есть проблемы — см. docker compose logs")

    def ask(self, question: str, default: str = "n") -> bool:
        """Спросить через /dev/tty: stdin занят потоком `curl … | bash`."""
        mark = "[Y/n]" if default == "y" else "[y/N]"
        yes = ("y", "yes", "д", "да")
        try:
            # /dev/tty нельзя открыть как r+ (не seekable) — открываем две стороны
            with open("/dev/tty", "w", encoding="utf-8", errors="replace") as out, \
                 open("/dev/tty", "r", encoding="utf-8", errors="replace") as inp:
                PANE.finish()
                out.write(f"\n  {C_BOLD}{question}{C_RESET} {C_DIM}{mark}{C_RESET}: ")
                out.flush()
                answer = inp.readline().strip().lower()
        except OSError:
            return default == "y"
        if not answer:
            return default == "y"
        return answer in yes

    HEALTH_TARGETS = (
        ("litellm", "http://127.0.0.1:4000/health/liveliness", True),
        ("open-webui", "http://127.0.0.1:8080/health", True),
        ("searxng", "http://127.0.0.1:8081/", False),
    )

    def step_health(self) -> None:
        """Живы ли сервисы: контейнеры, HTTP, Postgres, Redis. До установки Hermes."""
        step("Проверка доступности сервисов")
        if self.dry:
            info("[dry-run] проверил бы контейнеры, HTTP, Postgres и Redis")
            return

        critical_failed: list[str] = []
        warnings: list[str] = []

        services = sorted({svc for svc in self.compose_build_targets().values()} |
                          set(self.service_names()))
        for svc in services:
            res = self.run(
                ["docker", "inspect", svc, "--format",
                 "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}"],
                check=False, quiet=True,
            )
            if res.returncode != 0:
                warn(f"{svc}: контейнера нет")
                warnings.append(f"{svc}: нет контейнера")
                continue
            parts = (res.stdout or "").strip().split("|")
            state = parts[0] if parts else "?"
            health = parts[1] if len(parts) > 1 else ""
            restarts = parts[2] if len(parts) > 2 else "?"
            good = state == "running" and health in ("healthy", "")
            if good:
                ok(f"{svc}: {state} {health} restarts={restarts}".replace("  ", " "))
            else:
                warn(f"{svc}: {state} {health} restarts={restarts}")
                if svc in ("open-webui", "litellm", "litellm-redis"):
                    critical_failed.append(svc)

        for name, url, required in self.HEALTH_TARGETS:
            res = self.run(
                ["curl", "-fsS", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", url],
                check=False, quiet=True,
            )
            code = (res.stdout or "").strip()
            if code.startswith("2") or code.startswith("3"):
                ok(f"{name}: HTTP {code}")
            else:
                warn(f"{name}: HTTP {code or 'нет ответа'}")
                if required:
                    critical_failed.append(name)

        for db in ("webui_db", "litellm_db"):
            n = self.psql_scalar("SELECT count(*) FROM pg_tables WHERE schemaname='public'", db=db)
            if n.isdigit():
                ok(f"{db}: {n} таблиц")
            else:
                warn(f"{db}: недоступна")
                critical_failed.append(db)

        # пароль лежит в env самого контейнера — не тащим его через командную строку
        redis = self.run(
            ["docker", "exec", "litellm-redis", "sh", "-c",
             'redis-cli -a "$LITELLM_REDIS_PASSWORD" --no-auth-warning ping'],
            check=False, quiet=True,
        )
        if "PONG" in (redis.stdout or ""):
            ok("redis: PONG")
        else:
            warn("redis: не отвечает")
            critical_failed.append("redis")

        if self.args.skip_tls:
            info("TLS пропущен — домены не проверяю")

        if critical_failed:
            die("сервисы не отвечают: " + ", ".join(sorted(set(critical_failed)))
                + " — смотрите docker compose logs")
        ok("все сервисы отвечают")
        if warnings:
            self.failed.append("часть сервисов без контейнера: " + ", ".join(warnings))

    def service_names(self) -> list[str]:
        res = self.compose("config", "--services", quiet=True)
        names = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
        return [n for n in names if not n.startswith("_")]

    def fetch_real_hook(self, path: Path) -> bool:
        """Забрать рабочий хук из hermes-state, если локально оказался шаблон-заглушка."""
        if not path.is_file() or b"000000000" not in path.read_bytes():
            return False
        raw = (f"https://raw.githubusercontent.com/{HERMES_STATE_REPO}/"
               f"{HERMES_STATE_BRANCH}/hermes/agent-hooks/{path.name}")
        res = self.run(["curl", "-fsSL", "--max-time", "30", "-o", str(path), raw], check=False, quiet=True)
        if res.returncode != 0 or not path.is_file() or b"000000000" in path.read_bytes():
            warn(f"хук {path.name}: заглушка не заменилась — гейт никого не ограничивает")
            return False
        os.chmod(path, 0o755)
        ok(f"хук {path.name}: забрал рабочий файл из {HERMES_STATE_REPO}")
        return True

    def check_agent_hooks(self, config_path: Path | None = None) -> None:
        """Проверить хуки Hermes: битый хук с fail_closed блокирует все инструменты.

        Типовые поломки после переноса: нет бита исполнения, CRLF в shebang,
        интерпретатор из несуществующего venv.
        """
        cache = config_path or Path("/root/.hermes/config.yaml")
        if not cache.is_file():
            return
        text = cache.read_text(errors="replace")
        hooks = re.findall(r"^\s*-?\s*command:\s*[\"']?([^\"'\n]*agent-hooks[^\"'\n]*)[\"']?\s*$", text, re.M)
        hooks += re.findall(r"command:\s*[\"']?([^\"'\n]*\.py)[\"']?\s*$", text, re.M)
        for raw in sorted({h.strip() for h in hooks if "agent-hooks" in h}):
            path = Path(os.path.expanduser(raw))
            if not path.is_file():
                # Сначала пробуем восстановить сам хук из шаблона в скиллах: гейт
                # ограничивает конкретных пользователей, и молча его отключать нельзя.
                template = next(
                    (q for q in Path("/root/.hermes/skills").rglob(f"templates/{path.name}") if q.is_file()),
                    None,
                )
                if template is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, path)
                    os.chmod(path, 0o755)
                    warn(f"хук {path.name} восстановлен из шаблона — внутри ЗАГЛУШКА, гейт никого не ограничивает")
                    self.fetch_real_hook(path)
                    continue
                # Файла и шаблона нет: fail_closed-хук парализует агента, поэтому
                # комментируем ссылку, а оригинал конфига сохраняем рядом.
                backup = cache.with_suffix(".yaml.bak-hooks")
                if not backup.exists():
                    shutil.copyfile(cache, backup)
                lines = cache.read_text(errors="replace").splitlines()
                changed = False
                i = 0
                while i < len(lines):
                    line = lines[i]
                    if str(raw) in line and not line.lstrip().startswith("#"):
                        indent = len(line) - len(line.lstrip())
                        lines[i] = "# отключено установщиком: файл хука отсутствует\n# " + line
                        changed = True
                        # закомментировать и вложенные параметры записи (fail_closed и пр.)
                        j = i + 1
                        while j < len(lines):
                            nxt = lines[j]
                            if not nxt.strip():
                                j += 1
                                continue
                            if len(nxt) - len(nxt.lstrip()) <= indent:
                                break
                            lines[j] = "# " + nxt
                            j += 1
                        i = j
                        continue
                    i += 1
                if changed:
                    cache.write_text("\n".join(lines) + "\n")
                    warn(f"хук {path} отсутствовал — отключил его в config.yaml (бэкап: {backup.name})")
                else:
                    warn(f"хук {path} отсутствует и не отключился автоматически")
                continue
            body = path.read_bytes()
            if b"000000000" in body and self.fetch_real_hook(path):
                body = path.read_bytes()
            fixed = False
            if b"\r\n" in body:
                path.write_bytes(body.replace(b"\r\n", b"\n"))
                body = path.read_bytes()
                fixed = True
            mode = path.stat().st_mode
            if not mode & 0o111:
                os.chmod(path, mode | 0o755)
                fixed = True
            first = body.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
            if first.startswith("#!"):
                interp = first[2:].split()[0] if first[2:].split() else ""
                if interp == "/usr/bin/env":
                    target = first[2:].split()[1] if len(first[2:].split()) > 1 else ""
                    ok_interp = shutil.which(target) is not None
                else:
                    ok_interp = Path(interp).is_file()
                if not ok_interp:
                    path.write_text("#!/usr/bin/env python3\n" + body.split(b"\n", 1)[1].decode("utf-8", "replace"))
                    fixed = True
                    warn(f"хук {path.name}: починил shebang на /usr/bin/env python3")
            if fixed:
                ok(f"хук {path.name}: права/shebang поправлены")
            else:
                ok(f"хук {path.name}: в порядке")

    def ensure_hermes_shim(self) -> None:
        """Сделать hermes доступным как команда: установщик кладёт бинарь
        в ~/.local/bin или ~/.hermes/..., а PATH у root их не содержит."""
        found = shutil.which("hermes")
        if found:
            ok(f"hermes уже в PATH: {found}")
            return
        candidates = [
            Path("/root/.local/bin/hermes"),
            Path("/root/.hermes/hermes-agent/.hermes/bin/hermes"),
            Path("/root/.hermes/hermes-agent/hermes"),
        ]
        src = next((c for c in candidates if c.is_file()), None)
        if src is None:
            warn("после установки не нашёл бинарь hermes — смотрите вывод установщика")
            return
        try:
            os.chmod(src, 0o755)
        except OSError:
            pass
        target = Path("/usr/local/bin/hermes")
        if target.is_symlink() and Path(os.readlink(target)) == src:
            ok(f"hermes уже слинкован: {target}")
            return
        if target.exists() or target.is_symlink():
            warn(f"{target} занят — не трогаю, добавьте {src.parent} в PATH")
            return
        target.symlink_to(src)
        ok(f"hermes доступен как {target}")

    def ensure_hermes_gateway(self) -> None:
        """Гейтвей: user-юнит, если он есть, иначе — обычным процессом."""
        unit = "hermes-" + "gateway.service"
        units = self.run(["bash", "-c", f"systemctl --user list-unit-files {unit}"], check=False, quiet=True)
        if unit in (units.stdout or ""):
            self.run(["bash", "-c", f"XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart {unit}"], check=False)
            ok("гейтвей Hermes перезапущен через systemd")
            return
        running = self.run(["bash", "-c", "pgrep -f 'hermes gateway run'"], check=False, quiet=True)
        if (running.stdout or "").strip():
            ok("гейтвей Hermes уже работает")
            return
        self.run(["bash", "-c", "nohup /usr/local/bin/hermes gateway run >>/var/log/hermes-gateway.log 2>&1 &"],
                 check=False)
        ok("гейтвей Hermes запущен процессом (юнита нет)")

    def step_hermes(self) -> None:
        step("Hermes")
        if self.hermes_restore is None:
            # Явный запуск шага hermes (--only hermes) означает «развернуть состояние»,
            # если не сказано обратное: иначе шаг молча ничего не восстанавливает.
            self.hermes_restore = True
        if self.args.hermes is False:
            info("--no-hermes: пропускаю")
            return
        installed = shutil.which("hermes") is not None or any(
            c.is_file() for c in (
                Path("/root/.local/bin/hermes"),
                Path("/root/.hermes/hermes-agent/.hermes/bin/hermes"),
                Path("/root/.hermes/hermes-agent/hermes"),
            )
        )
        if not getattr(self.args, "no_ask_hermes", False):
            # Спрашиваем ВСЕГДА, даже когда шаг включён флагом --hermes и режим
            # автоматический: вопрос идёт через /dev/tty (stdin занят `curl … | bash`).
            if not self.ask("Установить Hermes (агент + харнесс) на этот сервер?", default="y"):
                info("пропущено — поставить позже: python3 install.py --only hermes --hermes")
                pending = Path("/root/.hermes-skip")
                if not self.dry:
                    pending.write_text("hermes не установлен при развёртывании\n")
                return
        if installed:
            ok(f"Hermes уже установлен: {shutil.which('hermes')}")
        else:
            info("запускаю официальный установщик Hermes (интерактивный)")
            if self.dry:
                info(f"[dry-run] curl -fsSL {HERMES_INSTALL_URL} | bash")
                return
            extra = os.environ.get("HERMES_INSTALL_ARGS")
            if extra is None:
                extra = "" if sys.stdin.isatty() else "--non-interactive"
            tail = f" -s -- {extra}" if extra.strip() else ""
            proc = subprocess.run(["bash", "-c", f"curl -fsSL {HERMES_INSTALL_URL} | bash{tail}"])
            if proc.returncode != 0:
                warn("установщик Hermes завершился с ошибкой — инфраструктура при этом готова")
                return
            ok("Hermes установлен")

        self.ensure_hermes_shim()
        if self.hermes_restore:
            self.restore_hermes_state()

        # Обвязка Hermes: boot-notify + бэкап состояния.
        expected = Path("/etc/hermes-boot-expected-containers.txt")
        if not expected.is_file() and not self.dry:
            expected.write_text("\n".join(["litellm", "open-webui", "searxng", "open-terminal", "litellm-redis"]) + "\n")
            self.install_units(["hermes-boot-notify.service"], ["hermes-boot-notify.service"])
        if self.env.get("GITHUB_KEY"):
            self.install_units(HERMES_UNITS, ["hermes-boot-notify.service", "hermes-state-watch.service",
                                              "hermes-state-backup.timer", "hermes-task-digest.timer",
                                              "hermes-task-board-watch.path"])
            ok("обвязка Hermes включена (boot-notify, state backup)")
        else:
            info("GITHUB_KEY не задан — бэкап состояния Hermes не настроен")

    def restore_tree(self, src: Path, dst: Path, owner: str) -> int:
        """Копирует дерево, сохраняя права файлов (скрипты остаются исполняемыми)."""
        if not src.is_dir():
            return 0
        n = 0
        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_dir():
                self.ensure_dir(target, 0o755, owner)
                continue
            mode = (item.stat().st_mode & 0o777) or 0o644
            self.ensure_dir(target.parent, 0o755, owner)
            if self.copy_file(item, target, mode, owner):
                n += 1
        return n

    def quarantine_extra_skills(self, src: Path, dest: Path) -> None:
        """Убрать скиллы, которых нет в восстановленном наборе.

        Официальный установщик Hermes кладёт десятки демо-скиллов. После
        восстановления из hermes-state они остаются и всплывают в списке как
        рабочие. Переносим лишнее в карантин (не удаляем), сверяя дерево
        рекурсивно: и категории, и скиллы внутри них.
        """
        if not src.is_dir() or not dest.is_dir():
            return
        vault = dest.parent / "skills_removed" / time.strftime("%Y%m%d_%H%M%S")
        moved = self._quarantine_walk(src, dest, vault)
        if moved:
            ok(f"лишние скиллы убраны в {vault} ({moved})")

    def _quarantine_walk(self, src: Path, dest: Path, vault: Path) -> int:
        """Рекурсивно перенести в vault то, чего нет в src."""
        keep = {p.name for p in src.iterdir()}
        moved = 0
        for path in list(dest.iterdir()):
            if path.name in keep:
                twin = src / path.name
                if path.is_dir() and twin.is_dir():
                    moved += self._quarantine_walk(twin, path, vault)
                continue
            if path.name == ".git":
                continue
            if self.dry:
                moved += 1
                continue
            vault.mkdir(parents=True, exist_ok=True)
            target = vault / path.name
            if target.exists():
                target = vault / f"{path.name}.{int(time.time())}"
            shutil.move(str(path), str(target))
            moved += 1
        return moved

    def restore_hermes_state(self) -> None:
        """Разворачивает config/skills/скрипты/юниты из приватного hermes-state."""
        step("Состояние Hermes из приватного репозитория")
        key = self.env.get("GITHUB_KEY", "")
        prefix = Path(os.environ.get("HERMES_RESTORE_ROOT", "/"))
        sandbox = prefix != Path("/")

        def dst(path: str | Path) -> Path:
            return prefix / str(path).lstrip("/")

        if self.dry:
            info(
                f"[dry-run] скачал бы {HERMES_STATE_REPO}@{HERMES_STATE_BRANCH} и разложил "
                f"hermes/ → {dst(HERMES_HOME)}, host/bin → /usr/local/bin, "
                "host/systemd → /etc/systemd/system, host/etc → /etc"
            )
            return

        work = STATE_DIR / "hermes-state"
        self.ensure_dir(work, 0o700)
        archive = work / "state.tar.gz"
        # Публичный репозиторий тянем через codeload (без токена и лимитов API),
        # приватный — через API с токеном.
        if key:
            url = f"https://api.github.com/repos/{HERMES_STATE_REPO}/tarball/{HERMES_STATE_BRANCH}"
            headers = {"Authorization": f"Bearer {key}"}
        else:
            url = f"https://codeload.github.com/{HERMES_STATE_REPO}/tar.gz/refs/heads/{HERMES_STATE_BRANCH}"
            headers = {}
        info(f"скачиваю {HERMES_STATE_REPO}@{HERMES_STATE_BRANCH}")
        try:
            archive.write_bytes(self.http_get(url, headers=headers, timeout=300))
        except (urllib.error.URLError, OSError) as exc:
            warn(f"репозиторий состояния недоступен: {exc} — продолжаю без него")
            return

        extract = work / "tree"
        self.ensure_dir(extract, 0o700)
        try:
            with tarfile.open(archive) as tar:
                try:
                    tar.extractall(extract, filter="data")
                except TypeError:  # Python < 3.12
                    tar.extractall(extract)
        except (tarfile.TarError, OSError) as exc:
            die(f"не удалось распаковать состояние Hermes: {exc}")
        roots = sorted(p for p in extract.iterdir() if p.is_dir())
        src = roots[0] if roots else None
        if src is None or not (src / "hermes").is_dir():
            die("в архиве состояния нет каталога hermes/ — проверьте HERMES_STATE_REPO")

        manifest = src / "MANIFEST.sha256"
        if manifest.is_file():
            res = self.run(
                ["bash", "-c", f"cd {shlex.quote(str(src))} && sha256sum -c --quiet MANIFEST.sha256"],
                check=False,
            )
            if res.returncode != 0:
                warn("MANIFEST.sha256 не сошёлся — разворачиваю как есть")
            else:
                ok("MANIFEST.sha256 сошёлся")

        copied = 0
        copied += self.restore_tree(src / "hermes", dst(HERMES_HOME), "root")
        self.quarantine_extra_skills(src / "hermes" / "skills", dst(HERMES_HOME) / "skills")
        copied += self.restore_tree(src / "host" / "bin", dst("/usr/local/bin"), "root")
        copied += self.restore_tree(src / "host" / "systemd", dst("/etc/systemd/system"), "root")
        # git не хранит права: возвращаем 0600 файлам, которые в снапшоте писались закрыто.
        for rel, mode in (("config.yaml", 0o600), ("SOUL.md", 0o600), ("cron/jobs.json", 0o600)):
            target = dst(HERMES_HOME) / rel
            if target.is_file():
                os.chmod(target, mode)

        etc_map = {
            "docker-autorecover.conf": "/etc/docker-autorecover.conf",
            "hermes-boot-expected-containers.txt": "/etc/hermes-boot-expected-containers.txt",
            "logrotate.d-hermes-state-backup": "/etc/logrotate.d/hermes-state-backup",
        }
        for name, target in etc_map.items():
            source = src / "host" / "etc" / name
            if source.is_file():
                self.copy_file(source, dst(target), 0o644, "root")
                copied += 1

        ai_src = src / "host" / "ai-stack"
        if (ai_src / "backup-databases.sh").is_file():
            self.copy_file(ai_src / "backup-databases.sh",
                           dst(AI_HOME / "ai-stack" / "ai" / "backup-databases.sh"), 0o755, AI_USER)
            copied += 1
        for runbook in ("runbook-containers.md", "runbook.md"):
            if (ai_src / runbook).is_file():
                self.copy_file(ai_src / runbook, dst(AI_HOME / "ai-stack" / runbook), 0o644, AI_USER)
                copied += 1

        registry = src / "host" / "backups" / "tg-registry.json"
        if registry.is_file():
            self.copy_file(registry, self.dump_dir / ".tg-registry.json", 0o644, AI_USER)
            copied += 1
            ok("реестр file_id дампов разложен рядом с бэкапами")

        ok(f"состояние Hermes развёрнуто: {copied} файлов → {dst(HERMES_HOME)}")
        if sandbox:
            info(f"HERMES_RESTORE_ROOT={prefix}: systemd не трогаю")
            return

        self.run(["systemctl", "daemon-reload"], check=False)
        self.run(["systemctl", "enable", "--now", "hermes-state-watch.service"], check=False)
        self.run(["loginctl", "enable-linger", "root"], check=False)
        self.ensure_hermes_gateway()
        self.check_agent_hooks()
        ok("hermes-state-watch включён")

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
        if self.args.secrets_from_bot or self.args.secrets_archive:
            self.restore_secrets()
            self.load_config()
        if getattr(self.args, "hermes_token", None) and not self.dry:
            self.override_hermes_token()
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
        PANE.finish()
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

        say()
        if self.failed:
            warn(f"завершено за {elapsed:.0f} c, есть проблемы: {'; '.join(self.failed)}")
            return 2
        ok(f"готово за {elapsed:.0f} c")
        info(f"ранбук проверки сервера: {ROOT / 'REPORT.md'} — отдайте его агенту или откройте раздел 1")
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
        say(f"  {C_DIM}·{C_RESET} {db}: {latest.name} ({latest.stat().st_size / 1024**2:.1f} MB)")

    with tarfile.open(out, "w:gz") as tar:
        for path, arcname in files:
            tar.add(path, arcname=arcname)
        if args.with_data:
            data = WEBUI_DATA
            if data.is_dir():
                say(f"  {C_DIM}·{C_RESET} webui_data: {sum(1 for _ in data.rglob('*'))} объектов")
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

    say()
    ok(f"bundle: {out} ({out.stat().st_size / 1024**2:.1f} MB)")
    info("перенесите файл на новый сервер и запустите:")
    info(f"  install.py --bundle {out.name} --restore")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


# ── интерактивный выбор флагов (запуск без аргументов) ──────────────────────

FLAGS_STORE = Path("/etc/vps-infra/install-flags.json")

MENU_ITEMS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("yes",         ("--yes",),               "Без вопросов",           "брать значения из существующих .env, ничего не спрашивать"),
    ("no_ask",      ("--no-ask-hermes",),     "Не спрашивать про Hermes", "не задавать вопрос об агенте, действовать по флагам"),
    ("dry",         ("--dry-run",),           "Только план",            "показать, что будет сделано, и выйти"),
    ("skip_tls",    ("--skip-tls",),          "Без TLS",                "не трогать nginx и сертификаты"),
    ("skip_rest",   ("--skip-restore",),      "Не трогать базы",        "пропустить восстановление БД"),
    ("restore",     ("--restore",),           "Восстановить поверх",    "применить дампы, даже если данные уже есть"),
    ("from_bot",    ("--from-bot",),          "Дампы из Telegram",      "взять последний бэкап БД из файл-бота"),
    ("secrets",     ("--secrets-from-bot",),  "Секреты из Telegram",    "разложить .env из зашифрованного архива"),
    ("rest_hermes", ("--restore-hermes",),    "Состояние Hermes",       "config/skills/скрипты из hermes-state"),
    ("hermes",      ("--hermes",),            "Установить Hermes",      "агент в хостовом режиме"),
    ("no_post",     ("--no-postcheck",),      "Без пост-проверки",      "не ставить таймер проверки через 15 минут"),
    ("no_report",   ("--no-report",),         "Без отчёта с секретами", "не отправлять архив .env в Telegram"),
    ("reconf",      ("--reconfigure",),       "Переспросить секреты",   "Enter — оставить текущее значение"),
)

# пары «взаимоисключающие»: включение одного гасит другое
MENU_EXCLUSIVE = (("skip_rest", "restore"), ("yes", "reconf"))


def load_saved_flags() -> set[str]:
    """Флаги прошлого запуска — чтобы чек-лист открывался с ними."""
    try:
        data = json.loads(FLAGS_STORE.read_text())
        saved = set(data.get("flags") or [])
    except (OSError, ValueError):
        return set()
    picked = set()
    for item_id, flags, _t, _d in MENU_ITEMS:
        if saved and all(f in saved for f in flags):
            picked.add(item_id)
    return picked


def save_flags(flags: list[str]) -> None:
    try:
        FLAGS_STORE.parent.mkdir(parents=True, exist_ok=True)
        FLAGS_STORE.write_text(
            json.dumps({"flags": flags, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                       ensure_ascii=False, indent=2) + "\n"
        )
        FLAGS_STORE.chmod(0o644)
    except OSError:
        pass


class FlagMenu:
    """Чек-лист флагов: ↑/↓ — курсор, пробел — галочка, Enter — подтверждение, q — выход."""

    CLEAR = "\033[H\033[2J"

    def __init__(self, argv_hint: str) -> None:
        self.argv_hint = argv_hint
        self.selected = load_saved_flags()
        self.cursor = 0
        self.stage = "list"

    # ── состояние ──
    def flags(self) -> list[str]:
        out: list[str] = []
        for item_id, flags, _title, _desc in MENU_ITEMS:
            if item_id in self.selected:
                out.extend(flags)
        return out

    def toggle(self) -> None:
        item_id = MENU_ITEMS[self.cursor][0]
        if item_id in self.selected:
            self.selected.discard(item_id)
            return
        self.selected.add(item_id)
        for a, b in MENU_EXCLUSIVE:
            if item_id == a:
                self.selected.discard(b)
            elif item_id == b:
                self.selected.discard(a)

    # ── отрисовка ──
    def draw(self, out) -> None:
        lines: list[str] = [self.CLEAR + "\033[?25l"]
        if self.stage == "list":
            lines.append(f"{C_BOLD}Установка инфраструктуры VPS{C_RESET}   {C_DIM}выбор флагов{C_RESET}")
            lines.append("")
            lines.append(f"{C_DIM}  ↑/↓ или j/k — двигаться · пробел — включить/выключить · Enter — дальше · q — выход{C_RESET}")
            lines.append("")
            for idx, (item_id, flags, title, _desc) in enumerate(MENU_ITEMS):
                mark = f"{C_GREEN}[x]{C_RESET}" if item_id in self.selected else f"{C_DIM}[ ]{C_RESET}"
                pointer = f"{C_CYAN}❯{C_RESET}" if idx == self.cursor else " "
                label = f"{C_BOLD}{title}{C_RESET}" if idx == self.cursor else title
                lines.append(f"  {pointer} {mark} {label} {C_DIM}({' '.join(flags)}){C_RESET}")
            lines.append("")
            chosen = self.flags()
            tail = " ".join(chosen) if chosen else "(без флагов — обычная интерактивная установка)"
            lines.append(f"{C_DIM}  итог:{C_RESET} python3 install.py {tail}")
        else:
            cmd = f"{self.argv_hint} " + (" ".join(self.flags()) if self.flags() else "")
            lines.append(f"{C_BOLD}Проверь и подтверди{C_RESET}")
            lines.append("")
            lines.append(f"  {C_CYAN}{cmd.strip()}{C_RESET}")
            lines.append("")
            picked = [t for i, _f, t, _d in MENU_ITEMS if i in self.selected]
            if picked:
                for title in picked:
                    lines.append(f"  {C_GREEN}✓{C_RESET} {title}")
            else:
                lines.append(f"  {C_DIM}флагов нет: установщик пойдёт обычным путём и задаст свои вопросы{C_RESET}")
            lines.append("")
            lines.append(f"{C_BOLD}  Y{C_RESET} — выполнить    {C_BOLD}n{C_RESET} — вернуться к выбору {C_DIM}(по умолчанию n){C_RESET}")
        out.write("\n".join(lines) + "\n")
        out.flush()

    # ── ввод ──
    @staticmethod
    def read_key(fd: int) -> str:
        ch = os.read(fd, 1)
        if ch == b"\x03":                      # Ctrl-C
            return "quit"
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b" ":
            return "space"
        if ch == b"\x1b":
            # стрелка приходит как ESC [ A — байты могут приехать не сразу
            seq = b""
            while len(seq) < 2:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    break
                seq += os.read(fd, 2 - len(seq))
            return {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left"}.get(seq, "esc")
        try:
            return ch.decode()
        except UnicodeDecodeError:
            return ""


def run_flag_menu(argv_hint: str) -> list[str] | None:
    """Возвращает список флагов либо None, если меню недоступно/отменено."""
    try:
        tty_out = open("/dev/tty", "w")
        tty_in = open("/dev/tty", "rb", buffering=0)
    except OSError:
        return None
    fd = tty_in.fileno()
    menu = FlagMenu(argv_hint)
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setraw(fd)
        while True:
            menu.draw(tty_out)
            key = menu.read_key(fd)
            if menu.stage == "list":
                if key in ("up", "k"):
                    menu.cursor = (menu.cursor - 1) % len(MENU_ITEMS)
                elif key in ("down", "j"):
                    menu.cursor = (menu.cursor + 1) % len(MENU_ITEMS)
                elif key in ("space", "x"):
                    menu.toggle()
                elif key == "enter":
                    menu.stage = "confirm"
                elif key in ("q", "quit", "esc"):
                    return "abort"
            else:
                if key in ("y", "Y", "н", "Н"):
                    flags = menu.flags()
                    save_flags(flags)
                    return flags
                if key in ("n", "N", "т", "Т", "enter", "q", "quit", "esc"):
                    menu.stage = "list"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        tty_out.write("\033[?25h\n")
        tty_out.flush()
        tty_in.close()
        tty_out.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Установщик инфраструктуры VPS (идемпотентный).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Шаги: " + ", ".join(STEP_NAMES) + "\n"
            "Примеры:\n"
            "  sudo python3 install.py            # интерактивный выбор флагов (чек-лист)\n"
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
    p.add_argument("--postcheck-delay", default="15min",
                   help="через сколько после развёртывания Hermes проверит сервер (systemd-формат, default 15min)")
    p.add_argument("--no-postcheck", dest="no_postcheck", action="store_true",
                   help="не ставить таймер пост-проверки")
    p.add_argument("--no-report", dest="no_report", action="store_true",
                   help="не отправлять отчёт с секретами в Telegram")
    p.add_argument("--no-ask-hermes", dest="no_ask_hermes", action="store_true",
                   help="не спрашивать про Hermes, действовать по флагам (для автоматизации)")
    p.add_argument("--skip", help="пропустить шаги (через запятую)")
    p.add_argument("--bundle", help="архив с дампами БД (путь или URL)")
    p.add_argument("--from-bot", action="store_true", help="взять последний бэкап БД из Telegram-бота")
    p.add_argument("--tg-token", help="токен файл-бота для --from-bot (иначе спросит / возьмёт из .env)")
    p.add_argument("--tg-registry", help="реестр file_id (путь или URL; по умолчанию — приватный hermes-state)")
    p.add_argument(
        "--secrets-from-bot",
        action="store_true",
        help="скачать зашифрованный архив .env из файл-бота (набор secrets_offsite) и разложить по путям",
    )
    p.add_argument(
        "--secrets-archive",
        help="локальный .enc архив секретов вместо скачивания из бота",
    )
    p.add_argument(
        "--secrets-key",
        help="файл с паролем архива секретов (по умолчанию /root/.secrets/offsite.key)",
    )
    p.add_argument("--bot-token", help="переопределить BOT_TOKEN (например, тестовый бот для прогона)")
    p.add_argument("--file-bot-token", help="переопределить FILE_BOT_TOKEN")
    p.add_argument("--hermes-token", help="переопределить TELEGRAM_BOT_TOKEN Hermes (тестовый бот)")
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="переопределить значение .env (можно несколько раз)",
    )
    p.add_argument("--restore", action="store_true", help="восстановить БД поверх существующих данных")
    p.add_argument("--force-stale", action="store_true", help=f"применять дампы старше {STALE_HOURS} ч")
    p.add_argument("--skip-restore", action="store_true", help="не трогать базы")
    p.add_argument("--skip-tls", action="store_true", help="не трогать nginx/certbot")
    p.add_argument("--hermes", dest="hermes", action="store_true", default=None, help="установить Hermes")
    p.add_argument("--no-hermes", dest="hermes", action="store_false", help="не устанавливать Hermes")
    p.add_argument("--restore-hermes", dest="restore_hermes", action="store_true", default=None,
                   help="развернуть config/skills/скрипты Hermes из приватного hermes-state")
    p.add_argument("--no-restore-hermes", dest="restore_hermes", action="store_false",
                   help="не разворачивать состояние Hermes")
    p.add_argument("--dry-run", action="store_true", help="показать план, ничего не менять")
    p.add_argument("--out", help="каталог для export")
    p.add_argument("--with-data", action="store_true", help="export: включить webui_data")
    p.add_argument("command", nargs="?", default="install", choices=["install", "check", "export"])
    return p


def main(argv: list[str]) -> int:
    if not argv:
        picked = run_flag_menu(f"python3 {Path(__file__).resolve()}")
        if picked == "abort":
            print("отменено")
            return 130
        if isinstance(picked, list):
            argv = picked
            if os.environ.get("VPS_MENU_PRINT"):
                print(json.dumps(argv))
                return 0

    args = build_parser().parse_args(argv)

    if args.command == "export":
        return do_export(args)

    if args.command == "check":
        args.check_mode = True
        args.yes = True
        args.skip = "packages,users,stack,secrets,bundle,fetch,postgres,network,images,migrate,restore,apps,notifications,units,tls,hermes"
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
