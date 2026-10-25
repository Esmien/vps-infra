#!/bin/bash
# vps-infra-push.sh — коммит и пуш изменений репозитория vps-infra.
#
# Логика повторяет hermes-state-backup.sh:
#   * один прогон за раз (flock);
#   * сначала подтягивает живые ассеты хоста (sync_assets.py);
#   * валидирует то, что коммитит (python/bash/compose), чтобы репо было рабочим;
#   * сканирует дерево на секреты и отказывается коммитить находки;
#   * коммитит только реальные изменения и пушит;
#   * если предыдущий пуш не прошёл — догоняет origin (catch-up);
#   * при ошибке пишет в лог и шлёт один алерт в Telegram (троттлинг 30 мин).
#
# Ручной запуск: systemctl start vps-infra-push.service
set -uo pipefail
umask 022

REPO=${REPO:-/root/vps-infra}
STATE_DIR=${STATE_DIR:-/var/lib/vps-infra-autopush}
LOG=${LOG:-/var/log/vps-infra-autopush.log}
ENV_FILE=${ENV_FILE:-/home/ai_service/ai-stack/ai/.env}
BRANCH=${BRANCH:-main}
REMOTE=${REMOTE:-https://github.com/Esmien/vps-infra.git}
SYNC_FROM_HOST=${SYNC_FROM_HOST:-1}

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/push.lock"
flock -w 900 9 || { echo "another push is running" >&2; exit 1; }

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG"; }

notify_fail() {
    local msg=$1
    if command -v hermes >/dev/null 2>&1; then
        timeout 90 hermes send -t telegram "❌ vps-infra: $msg" >/dev/null 2>&1 && return 0
    fi
    local env=/home/ai_service/notifications/.env token owner
    [[ -r "$env" ]] || return 0
    token=$(grep -m1 '^BOT_TOKEN=' "$env" 2>/dev/null | cut -d= -f2-)
    owner=$(grep -m1 '^BOT_OWNER=' "$env" 2>/dev/null | cut -d= -f2-)
    [[ -n "$token" && -n "$owner" ]] || return 0
    curl -sS -m 15 -o /dev/null -X POST "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$owner" --data-urlencode "text=❌ vps-infra: $msg" || true
}

die() {
    local msg=$1 stamp="$STATE_DIR/.last-alert" now last=0
    now=$(date +%s)
    [[ -f "$stamp" ]] && last=$(cat "$stamp" 2>/dev/null || echo 0)
    [[ -z "$last" ]] && last=0
    log "ERROR: $msg"
    if (( now - last >= 1800 )); then
        printf '%s\n' "$now" >"$stamp"
        notify_fail "$msg"
    else
        log "alert throttled ($(( (now - last) / 60 )) min since last)"
    fi
    exit 1
}

[[ -d "$REPO/.git" ]] || { log "FATAL: $REPO is not a git repo"; exit 1; }
cd "$REPO" || exit 1

git config user.name  >/dev/null || git config user.name  "VPS Admin"
git config user.email >/dev/null || git config user.email "infra@$(hostname -s)"

# ---------------------------------------------------------------- credentials
ASKPASS="$STATE_DIR/git-askpass.sh"
if [[ -r "$ENV_FILE" ]]; then
    GITHUB_KEY=$(grep -m1 '^GITHUB_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    GITHUB_KEY=${GITHUB_KEY%\"}; GITHUB_KEY=${GITHUB_KEY#\"}
    GITHUB_KEY=${GITHUB_KEY%\'}; GITHUB_KEY=${GITHUB_KEY#\'}
fi
if [[ -n "${GITHUB_KEY:-}" ]]; then
    printf '%s\n' '#!/bin/sh' \
        'case "$1" in' \
        '  *[Uu]sername*) printf "%s\n" "x-access-token" ;;' \
        '  *) printf "%s\n" "${GITHUB_KEY:-}" ;;' \
        'esac' >"$ASKPASS"
    chmod 700 "$ASKPASS"
    export GITHUB_KEY
    GIT_ENV=(env GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0)
else
    GIT_ENV=(env GIT_TERMINAL_PROMPT=0)
fi

git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE"
git remote set-url origin "$REMOTE"

log "=== push start ==="

# Чужие коммиты (правка через веб) подтягиваем ДО синхронизации: иначе rebase
# упрётся в грязное дерево после sync_assets.py.
if ! "${GIT_ENV[@]}" git fetch -q origin "$BRANCH" 2>>"$LOG"; then
    die "git fetch failed (network or token)"
fi
if git rev-parse --verify -q "origin/$BRANCH" >/dev/null \
   && ! git merge-base --is-ancestor "origin/$BRANCH" HEAD; then
    log "origin/$BRANCH ahead — rebasing"
    git rebase --autostash "origin/$BRANCH" >>"$LOG" 2>&1 || die "rebase conflict, resolve manually"
fi

# ---------------------------------------------------------------- sync + validate
if [[ "$SYNC_FROM_HOST" == "1" ]]; then
    python3 maintainer/sync_assets.py >>"$LOG" 2>&1 || die "sync_assets.py failed"
fi

python3 -m py_compile install.py || die "install.py does not compile"
bash -n bootstrap.sh || die "bootstrap.sh syntax error"
bash -n maintainer/vps-infra-push.sh || die "vps-infra-push.sh syntax error"
bash -n maintainer/vps-infra-watch.sh || die "vps-infra-watch.sh syntax error"

# compose-интерполяция на фиктивных секретах: ловит битый YAML и забытые ${VAR}
if command -v docker >/dev/null 2>&1; then
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    cp -a assets/stack "$TMP/stack"
    sed -E 's/@@[A-Z0-9_]+@@/dummy-dummy-dummy-dummy-dummy-dummy/g' \
        assets/templates/ai.env.tmpl >"$TMP/stack/.env"
    if ! docker compose -f "$TMP/stack/docker-compose.yml" --project-directory "$TMP/stack" config -q >>"$LOG" 2>&1; then
        die "docker compose config failed on assets/stack"
    fi
fi

# ---------------------------------------------------------------- secret guard
SECRET_RE='(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|xoxb-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|[0-9]{9,10}:AA[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
HITS=$(grep -rInE "$SECRET_RE" . --exclude-dir=.git 2>/dev/null \
        | grep -vE '(xxx|XXX|\.\.\.|YOUR_|<[A-Z_]{3,}>|REDACTED|example|placeholder)' || true)
if [[ -n "$HITS" ]]; then
    log "SECRET-LIKE STRINGS FOUND — commit aborted:"
    echo "$HITS" | head -n 5 | cut -c1-200 | tee -a "$LOG" >&2
    die "$(echo "$HITS" | wc -l) secret-like string(s) in repo"
fi

# ---------------------------------------------------------------- commit + push
git add -A
git diff --cached --quiet && CHANGED=0 || CHANGED=1

remote_head() {
    "${GIT_ENV[@]}" git ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}' | head -n1
}

if (( CHANGED )); then
    STAT=$(git diff --cached --shortstat | sed -E 's/^ +//')
    FILES=$(git diff --cached --name-only | wc -l)
    git commit -q -m "infra: auto-sync $(date -u '+%Y-%m-%d %H:%M UTC')" \
        -m "$STAT" -m "$FILES file(s) changed" || die "git commit failed"
    log "committed $(git rev-parse --short HEAD): $STAT"
fi

LOCAL=$(git rev-parse HEAD)
if [[ "$(remote_head)" != "$LOCAL" ]]; then
    if "${GIT_ENV[@]}" git push -q origin "HEAD:$BRANCH" 2>>"$LOG"; then
        log "✓ pushed $LOCAL to $REMOTE"
    else
        die "git push failed (commit stays local, watcher will retry)"
    fi
else
    log "no changes — origin is up to date ($LOCAL)"
fi

log "=== push ok ==="
exit 0
