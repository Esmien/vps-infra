#!/bin/bash
# Ежедневный снапшот файловой памяти Open WebUI: только каталоги .agents всех
# пользователей из /home/ai_service/sandbox/users -> приватный репозиторий
# users_ai_memory на GitHub. Секретов в .agents нет, поэтому копируется целиком.
set -euo pipefail
umask 077

ENV_FILE="${ENV_FILE:-/home/ai_service/ai-stack/ai/.env}"
SRC_ROOT="/home/ai_service/sandbox/users"
STATE_DIR="/var/lib/ai-memory-repo"
REPO_DIR="$STATE_DIR/repo"
STAGE_DIR="$STATE_DIR/stage"
LOG_FILE="/var/log/ai-memory-backup.log"
GH_OWNER="${GH_OWNER:-Esmien}"
GH_REPO="${GH_REPO:-users_ai_memory}"
GIT_REMOTE="https://github.com/$GH_OWNER/$GH_REPO.git"
ASKPASS="$STATE_DIR/git-askpass.sh"
TG_TARGET="${TG_TARGET:-telegram:-1004410657647:10}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

exec 9>"$STATE_DIR/.backup.lock"
flock -w 600 9 || { echo "another memory backup is already running" >&2; exit 1; }

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"; }

send_tg() {
    command -v hermes >/dev/null 2>&1 || return 0
    timeout 90 hermes send -t "$TG_TARGET" "$1" >/dev/null 2>&1 || true
}

die() { log "ERROR: $*"; send_tg "❌ Память пользователей: бэкап не выполнен — $*"; exit 1; }

[[ -r "$ENV_FILE" ]] || die "cannot read $ENV_FILE"
GITHUB_KEY="$(grep -m1 -E '^GITHUB_KEY=' "$ENV_FILE" 2>/dev/null || true)"
GITHUB_KEY="${GITHUB_KEY#GITHUB_KEY=}"
GITHUB_KEY="${GITHUB_KEY%\"}"; GITHUB_KEY="${GITHUB_KEY#\"}"
GITHUB_KEY="${GITHUB_KEY%\'}"; GITHUB_KEY="${GITHUB_KEY#\'}"
[[ -n "$GITHUB_KEY" ]] || die "GITHUB_KEY missing in $ENV_FILE"
export GITHUB_KEY

printf '%s\n' '#!/bin/sh' \
    'case "$1" in' \
    '  *[Uu]sername*) printf "%s\n" "x-access-token" ;;' \
    '  *) printf "%s\n" "${GITHUB_KEY:-}" ;;' \
    'esac' > "$ASKPASS"
chmod 700 "$ASKPASS"

log "=== memory snapshot start ==="

# ---------------------------------------------------------------- repo + remote
if [[ ! -d "$REPO_DIR/.git" ]]; then
    log "initialising git repo at $REPO_DIR"
    mkdir -p "$REPO_DIR"
    git -C "$REPO_DIR" init -q -b main
fi
git -C "$REPO_DIR" config user.name  >/dev/null || git -C "$REPO_DIR" config user.name  "Hermes VPS Agent"
git -C "$REPO_DIR" config user.email >/dev/null || git -C "$REPO_DIR" config user.email "hermes@$(hostname -s)"
git -C "$REPO_DIR" config core.excludesFile /dev/null || true

if ! git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$REPO_DIR" remote add origin "$GIT_REMOTE"
fi

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
    -H "Authorization: Bearer $GITHUB_KEY" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$GH_OWNER/$GH_REPO")"
if [[ "$code" == "404" ]]; then
    log "creating private repo $GH_OWNER/$GH_REPO"
    curl -s --max-time 30 -X POST \
        -H "Authorization: Bearer $GITHUB_KEY" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/user/repos" \
        -d "{\"name\":\"$GH_REPO\",\"private\":true,\"description\":\"Файловая память пользователей Open WebUI (.agents)\",\"auto_init\":false}" \
        >/dev/null || die "cannot create repo $GH_REPO"
elif [[ "$code" != "200" ]]; then
    die "GitHub API returned $code for $GH_OWNER/$GH_REPO"
fi

# ---------------------------------------------------------------- staging
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/users"
users=0
for dir in "$SRC_ROOT"/*/; do
    slug="$(basename "$dir")"
    [[ -d "${dir}.agents" ]] || continue
    mkdir -p "$STAGE_DIR/users/$slug"
    rsync -a --delete "${dir}.agents" "$STAGE_DIR/users/$slug/"
    users=$((users + 1))
done

[[ "$users" -gt 0 ]] || die "no .agents directories found in $SRC_ROOT"
rsync -a --delete "$STAGE_DIR/users/" "$REPO_DIR/users/"
rm -rf "$STAGE_DIR"

# служебный README, чтобы репозиторий был понятен без контекста
cat > "$REPO_DIR/README.md" <<'EOF'
# users_ai_memory

Ежедневный снапшот файловой памяти пользователей Open WebUI.

- `users/<slug>/.agents/AGENTS.md` — правила и стиль поведения модели у этого пользователя.
- `users/<slug>/.agents/PROFILE.md` — устойчивые сведения о человеке.
- `users/<slug>/.agents/MEMORY.md` — заметки, проекты, договорённости.
- `.memory-state.json` — служебное состояние фильтра памяти (переполнение, конфликты).

Источник: `/home/ai_service/sandbox/users/<slug>/.agents` на VPS.
Обновляется systemd-таймером `ai-memory-backup.timer` ежедневно в 00:00 UTC.
EOF

cd "$REPO_DIR"
git add -A
changed="$(git diff --cached --name-only | wc -l)"
if [[ "$changed" -eq 0 ]]; then
    log "no changes; nothing to commit"
    send_tg "🧠 Память пользователей: изменений нет (${users} аккаунтов), пуш не нужен."
    exit 0
fi

git -c core.askpass="$ASKPASS" commit -q -m "memory snapshot $(date -u '+%Y-%m-%d %H:%M UTC')"
sha="$(git rev-parse --short HEAD)"

if ! GIT_ASKPASS="$ASKPASS" git -c core.askpass="$ASKPASS" push -q origin main 2>>"$LOG_FILE"; then
    die "git push failed"
fi

size="$(du -sh "$REPO_DIR/.git" | cut -f1)"
log "pushed $sha ($changed files, $users users, repo $size)"
send_tg "🧠 Память пользователей сохранена: ${users} аккаунтов, изменено файлов: ${changed}, коммит ${sha}."
