#!/usr/bin/env bash
#
# bootstrap.sh — одна команда для полного развёртывания.
#
#   curl -fsSL https://raw.githubusercontent.com/Esmien/vps-bootstrap/main/bootstrap.sh | sudo bash
#
# Перед запуском положить файл со стартовыми секретами (по умолчанию
# /root/bootstrap.env, иначе /tmp/bootstrap.env или ./bootstrap.env):
#
#   GITHUB_TOKEN=ghp_...            # клон приватного репозитория + доступ к hermes-state
#   SECRETS_KEY=<пароль архива .env>
#   TG_FILE_TOKEN=123456:AA...      # РЕАЛЬНЫЙ файл-бот: из него тянутся секреты и дампы
#   BOT_TOKEN=...                   # (необязательно) свой бот уведомлений
#   FILE_BOT_TOKEN=...              # (необязательно) свой файл-бот для этого сервера
#   HERMES_BOT_TOKEN=...            # (необязательно) свой бот Hermes
#   WEBUI_URL=http://127.0.0.1:8080 # (необязательно)
#
# Всё остальное скрипт делает сам: ставит зависимости, клонирует репозиторий,
# восстанавливает секреты из зашифрованного архива, поднимает стек, данные,
# мониторинг, таймеры и Hermes с харнессом. TLS пропускается по умолчанию —
# на чистой машине без домена он всё равно не выпустится.
#
# Переменные окружения:
#   VPS_BOOTSTRAP_ENV   путь к файлу стартовых секретов
#   VPS_INFRA_REPO      репозиторий (default: github.com/Esmien/vps-infra)
#   VPS_INFRA_BRANCH    ветка (default: main)
#   VPS_INFRA_DIR       каталог установки (default: /opt/vps-infra)
#   SECRETS_KEY_PATH    куда положить пароль архива (default: /root/.secrets/offsite.key)
#   WITH_TLS=1          не пропускать шаг nginx/certbot
#   BOOTSTRAP_DRY=1     только план (--dry-run)

set -Eeuo pipefail

REPO_URL="${VPS_INFRA_REPO:-https://github.com/Esmien/vps-infra.git}"
BRANCH="${VPS_INFRA_BRANCH:-main}"
DEST="${VPS_INFRA_DIR:-/opt/vps-infra}"
KEY_PATH="${SECRETS_KEY_PATH:-/root/.secrets/offsite.key}"
LOG=/var/log/vps-bootstrap.log

red=$'\033[1;31m'; grn=$'\033[1;32m'; cyn=$'\033[1;36m'; rst=$'\033[0m'
say() { printf '%s==>%s %s\n' "$cyn" "$rst" "$*"; }
ok()  { printf '%s ✓%s %s\n' "$grn" "$rst" "$*"; }
die() { printf '%sОШИБКА:%s %s\n' "$red" "$rst" "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "нужен root: curl … | sudo bash"

# ── лог: и в файл, и на экран ────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG")"
# Свой stdout не перехватываем: установщик рисует живой блок вывода и ему нужен tty.
# В файл уходит stderr + собственный лог установщика (/var/log/vps-infra-install.log).
: >> "$LOG"
say "начало: $(date -u '+%Y-%m-%d %H:%M:%S UTC'), лог установщика: /var/log/vps-infra-install.log"

# ── стартовые секреты ───────────────────────────────────────────────────────
ENV_FILE="${VPS_BOOTSTRAP_ENV:-}"
if [[ -z "$ENV_FILE" ]]; then
    for cand in /root/bootstrap.env /tmp/bootstrap.env /home/*/bootstrap.env ./bootstrap.env; do
        [[ -f "$cand" ]] && { ENV_FILE="$cand"; break; }
    done
fi

# Вопрос через /dev/tty: stdin занят потоком `curl … | bash`.
tty_ask() {
    local prompt="$1" secret="${2:-0}" line=""
    [[ -e /dev/tty ]] || { printf ''; return 0; }
    printf '  %s: ' "$prompt" >/dev/tty
    if [[ "$secret" == "1" ]]; then
        stty -echo </dev/tty
        IFS= read -r line </dev/tty || true
        stty echo </dev/tty
        printf '\n' >/dev/tty
    else
        IFS= read -r line </dev/tty || true
    fi
    printf '%s' "$line"
}

INTERACTIVE=0
if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
    say "стартовые секреты: $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    chmod 600 "$ENV_FILE" 2>/dev/null || true
else
    INTERACTIVE=1
    say "файла стартовых секретов нет — спрошу необходимое (Enter — оставить пустым)"
fi

if [[ -z "${TG_FILE_TOKEN:-}" && "$INTERACTIVE" == "1" ]]; then
    TG_FILE_TOKEN="$(tty_ask 'Токен файл-бота (TG_FILE_TOKEN)' 1)"
    export TG_FILE_TOKEN
fi
if [[ -z "${SECRETS_KEY:-}" && "$INTERACTIVE" == "1" ]]; then
    SECRETS_KEY="$(tty_ask 'Пароль архива секретов (SECRETS_KEY)' 1)"
    export SECRETS_KEY
fi

# GITHUB_TOKEN и GITHUB_KEY — одно и то же, принимаем любой из вариантов
if [[ -z "${GITHUB_TOKEN:-}" && -n "${GITHUB_KEY:-}" ]]; then
    GITHUB_TOKEN="$GITHUB_KEY"
    export GITHUB_TOKEN
fi

missing_vars=()
# GITHUB_TOKEN необязателен: репозиторий публичный
[[ -n "${SECRETS_KEY:-}" ]]     || missing_vars+=("SECRETS_KEY")
[[ -n "${TG_FILE_TOKEN:-}" ]]   || missing_vars+=("TG_FILE_TOKEN")
if (( ${#missing_vars[@]} )); then
    printf '\n  в %s нет переменных:\n' "$ENV_FILE" >&2
    for v in "${missing_vars[@]}"; do printf '    - %s\n' "$v" >&2; done
    printf '\n  в файле сейчас: %s\n' "$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | tr -d '=' | paste -sd' ' -)" >&2
    printf '  формат: KEY=VALUE, без кавычек, по одной на строку\n\n' >&2
    die "стартовые секреты заполнены не полностью"
fi

[[ -z "${TG_FILE_TOKEN:-}" && -n "${FILE_BOT_TOKEN:-}" ]] && TG_FILE_TOKEN="$FILE_BOT_TOKEN" && export TG_FILE_TOKEN

export DEBIAN_FRONTEND=noninteractive

# ── зависимости ─────────────────────────────────────────────────────────────
missing=()
for bin in git curl python3 tar openssl; do
    command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
if (( ${#missing[@]} )); then
    say "ставлю зависимости: ${missing[*]}"
    apt-get update -qq
    pkgs=()
    for b in "${missing[@]}"; do
        case "$b" in
            python3) pkgs+=(python3 python3-venv) ;;
            git)     pkgs+=(git) ;;
            *)       pkgs+=("$b") ;;
        esac
    done
    apt-get install -y -qq --no-install-recommends ca-certificates "${pkgs[@]}"
fi
ok "зависимости на месте"

# ── репозиторий (приватный: клон по токену) ─────────────────────────────────
# Репозиторий публичный: токен нужен только для приватного. Авторизуемся
# заголовком, чтобы креды не попадали ни в URL, ни в .git/config.
AUTH_ARGS=()
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    _basic="$(printf '%s' "x-access-token:${GITHUB_TOKEN}" | base64 -w0)"
    AUTH_ARGS=(-c "http.extraheader=Authorization: Basic ${_basic}")
fi
AUTH_URL="$REPO_URL"
if [[ -d "$DEST/.git" ]]; then
    say "обновляю $DEST"
    git -C "$DEST" remote set-url origin "$AUTH_URL"
    git -C "$DEST" "${AUTH_ARGS[@]}" fetch --depth=1 origin "$BRANCH" --quiet
    git -C "$DEST" reset --hard FETCH_HEAD --quiet
    git -C "$DEST" clean -fdq -e '*.env' -e 'bundle*'
else
    say "клонирую репозиторий → $DEST"
    mkdir -p "$(dirname "$DEST")"
    git "${AUTH_ARGS[@]}" clone --depth=1 --branch "$BRANCH" "$AUTH_URL" "$DEST" --quiet \
        || die "не удалось склонировать $REPO_URL (проверьте сеть и доступ)"
fi
# токен не должен остаться в .git/config
git -C "$DEST" remote set-url origin "$REPO_URL"
cd "$DEST"
[[ -f install.py ]] || die "в репозитории нет install.py"
ok "репозиторий готов: $(git -C "$DEST" rev-parse --short HEAD)"

# ── пароль архива секретов ──────────────────────────────────────────────────
install -d -m 700 "$(dirname "$KEY_PATH")"
umask 077
printf '%s\n' "$SECRETS_KEY" > "$KEY_PATH"
chmod 600 "$KEY_PATH"
ok "пароль архива записан: $KEY_PATH"

# ── аргументы установщика ───────────────────────────────────────────────────
args=(--secrets-from-bot --secrets-key "$KEY_PATH"
      --tg-token "$TG_FILE_TOKEN" --from-bot
      --hermes --restore-hermes)
# В интерактивном режиме НЕ глотаем вопросы установщика.
[[ "$INTERACTIVE" == "1" ]] || args=(--yes "${args[@]}")
[[ "${WITH_TLS:-0}" == "1" ]] || args+=(--skip-tls)
[[ -n "${BOT_TOKEN:-}" ]]      && args+=(--bot-token "$BOT_TOKEN")
[[ -n "${FILE_BOT_TOKEN:-}" ]] && args+=(--file-bot-token "$FILE_BOT_TOKEN")
[[ -n "${HERMES_BOT_TOKEN:-}" ]] && args+=(--hermes-token "$HERMES_BOT_TOKEN")
[[ -n "${WEBUI_URL:-}" ]]      && args+=(--set "WEBUI_URL=$WEBUI_URL")
[[ -n "${EXTRA_ARGS:-}" ]]     && read -r -a extra <<< "$EXTRA_ARGS" && args+=("${extra[@]}")
[[ "${BOOTSTRAP_DRY:-0}" == "1" ]] && args+=(--dry-run)

export GITHUB_KEY="${GITHUB_TOKEN:-${GITHUB_KEY:-}}"   # токен необязателен: репо публичные
export HERMES_INSTALL_ARGS="${HERMES_INSTALL_ARGS:---non-interactive}"

say "запускаю установщик (${#args[@]} аргументов)"
say "дальше без участия: можно закрывать вкладку, прогресс в $LOG"
if python3 "$DEST/install.py" "${args[@]}" 2> >(tee -a "$LOG" >&2); then
    ok "готово: $(date -u '+%H:%M:%S UTC')"
else
    die "установщик завершился с ошибкой — смотрите $LOG"
fi
