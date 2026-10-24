#!/usr/bin/env bash
#
# bootstrap.sh — точка входа для `curl … | bash`.
#
#   curl -fsSL https://raw.githubusercontent.com/Esmien/vps-infra/main/bootstrap.sh | sudo bash
#
# Скрипт ставит git/curl/python3, забирает репозиторий в /opt/vps-infra
# и передаёт управление install.py (интерактивно). Аргументы пробрасываются:
#
#   … | sudo bash -s -- --yes
#   … | sudo bash -s -- --bundle https://…/bundle.tar.gz --restore
#
# Переменные окружения:
#   VPS_INFRA_REPO   URL репозитория (default: github.com/Esmien/vps-infra)
#   VPS_INFRA_BRANCH ветка (default: main)
#   VPS_INFRA_DIR    каталог установки (default: /opt/vps-infra)

set -Eeuo pipefail

REPO_URL="${VPS_INFRA_REPO:-https://github.com/Esmien/vps-infra.git}"
BRANCH="${VPS_INFRA_BRANCH:-main}"
DEST="${VPS_INFRA_DIR:-/opt/vps-infra}"

red=$'\033[1;31m'; grn=$'\033[1;32m'; cyn=$'\033[1;36m'; rst=$'\033[0m'
say() { printf '%s==>%s %s\n' "$cyn" "$rst" "$*"; }
die() { printf '%sОШИБКА:%s %s\n' "$red" "$rst" "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "нужен root: curl … | sudo bash"

export DEBIAN_FRONTEND=noninteractive

missing=()
for bin in git curl python3; do
    command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
if (( ${#missing[@]} )); then
    say "ставлю зависимости: ${missing[*]}"
    apt-get update -qq
    pkgs=()
    for b in "${missing[@]}"; do
        case "$b" in
            python3) pkgs+=(python3 python3-venv) ;;
            *)       pkgs+=("$b") ;;
        esac
    done
    apt-get install -y -qq --no-install-recommends ca-certificates "${pkgs[@]}"
fi

if [[ -d "$DEST/.git" ]]; then
    say "обновляю $DEST"
    git -C "$DEST" remote set-url origin "$REPO_URL"
    git -C "$DEST" fetch --depth=1 origin "$BRANCH"
    git -C "$DEST" reset --hard FETCH_HEAD --quiet
    git -C "$DEST" clean -fdq -e '*.env' -e 'bundle*'
else
    say "клонирую $REPO_URL → $DEST"
    mkdir -p "$(dirname "$DEST")"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$DEST" \
        || die "не удалось склонировать $REPO_URL (репозиторий приватный? задайте VPS_INFRA_REPO)"
fi

cd "$DEST"
[[ -f install.py ]] || die "в репозитории нет install.py"

# stdin — это труба curl. Возвращаем терминал, иначе вопросы установщика
# упрутся в EOF. Это последняя команда bash, дальше управление у Python.
if [[ ! -t 0 ]]; then
    if ( exec 3</dev/tty ) 2>/dev/null; then
        exec </dev/tty
    else
        printf '%s!%s нет терминала — вопросы установщика недоступны (нужен интерактивный запуск)\n' "$red" "$rst" >&2
    fi
fi

say "запускаю установщик"
exec python3 "$DEST/install.py" "$@"
