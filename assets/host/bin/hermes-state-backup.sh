#!/bin/bash
# Snapshot of everything that makes this Hermes install non-stock:
#   config, memories, skills, cron jobs, plugins/hooks/scripts,
#   ops scripts, systemd units, /etc state files.
# Commits to a local git repo and pushes to a private GitHub repo.
# Secrets are NOT copied (.hermes/.env, ai-stack/.env, tokens, keys).
set -euo pipefail
umask 077

ENV_FILE="${ENV_FILE:-/home/ai_service/ai-stack/ai/.env}"
STATE_DIR="/var/lib/hermes-state"
REPO_DIR="$STATE_DIR/repo"
LOG_FILE="/var/log/hermes-state-backup.log"
HERMES_HOME="${HERMES_HOME:-/root/.hermes}"
GH_OWNER="Esmien"
GH_REPO="hermes-state"
GIT_REMOTE="https://github.com/$GH_OWNER/$GH_REPO.git"
ASKPASS="$STATE_DIR/git-askpass.sh"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

# один снапшот за раз: вотчер и ручной запуск не должны переплетаться
exec 9>"$STATE_DIR/.snapshot.lock"
flock -w 900 9 || { echo "another snapshot is already running" >&2; exit 1; }

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"; }

notify_fail() {
    command -v hermes >/dev/null 2>&1 || return 0
    timeout 90 hermes send -t telegram "❌ Hermes state backup: $*" >/dev/null 2>&1 || true
}

die() { log "ERROR: $*"; notify_fail "$*"; exit 1; }

# ---------------------------------------------------------------- credentials
[[ -r "$ENV_FILE" ]] || die "cannot read $ENV_FILE"
GITHUB_KEY="$(grep -m1 -E '^GITHUB_KEY=' "$ENV_FILE" 2>/dev/null || true)"
GITHUB_KEY="${GITHUB_KEY#GITHUB_KEY=}"
GITHUB_KEY="${GITHUB_KEY%\"}"; GITHUB_KEY="${GITHUB_KEY#\"}"
GITHUB_KEY="${GITHUB_KEY%\'}"; GITHUB_KEY="${GITHUB_KEY#\'}"
[[ -n "$GITHUB_KEY" ]] || die "GITHUB_KEY missing in $ENV_FILE"
export GITHUB_KEY

# git asks for credentials through this helper: token never lands in .git/config
# or in argv, only in the environment of the git process.
printf '%s\n' '#!/bin/sh' \
    'case "$1" in' \
    '  *[Uu]sername*) printf "%s\n" "x-access-token" ;;' \
    '  *) printf "%s\n" "${GITHUB_KEY:-}" ;;' \
    'esac' > "$ASKPASS"
chmod 700 "$ASKPASS"

log "=== Hermes state snapshot start ==="

# ---------------------------------------------------------------- repo + remote
if [[ ! -d "$REPO_DIR/.git" ]]; then
    log "initialising git repo at $REPO_DIR"
    mkdir -p "$REPO_DIR"
    git -C "$REPO_DIR" init -q -b main
fi
git -C "$REPO_DIR" config user.name  >/dev/null || git -C "$REPO_DIR" config user.name  "Hermes VPS Agent"
git -C "$REPO_DIR" config user.email >/dev/null || git -C "$REPO_DIR" config user.email "hermes@$(hostname -s)"
git -C "$REPO_DIR" config core.excludesFile /dev/null || true

api_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
        -H "Authorization: Bearer $GITHUB_KEY" \
        -H "Accept: application/vnd.github+json" "$1"
}

code="$(api_code "https://api.github.com/repos/$GH_OWNER/$GH_REPO")" || die "GitHub API unreachable"
if [[ "$code" == "404" ]]; then
    log "remote repo $GH_OWNER/$GH_REPO not found, creating private repo"
    create_code="$(curl -s -o "$STATE_DIR/create-repo.json" -w '%{http_code}' --max-time 30 \
        -X POST -H "Authorization: Bearer $GITHUB_KEY" \
        -H "Accept: application/vnd.github+json" \
        -d "{\"name\":\"$GH_REPO\",\"private\":true,\"description\":\"Hermes agent state: config, skills, runbooks, ops scripts (auto-snapshots)\"}" \
        https://api.github.com/user/repos)"
    case "$create_code" in
        201|200) log "✓ created private repo https://github.com/$GH_OWNER/$GH_REPO" ;;
        *)       die "repo creation failed (HTTP $create_code)" ;;
    esac
elif [[ "$code" != "200" ]]; then
    die "GitHub API returned HTTP $code for repo lookup"
fi

if git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$REPO_DIR" remote set-url origin "$GIT_REMOTE"
else
    git -C "$REPO_DIR" remote add origin "$GIT_REMOTE"
fi

# ---------------------------------------------------------------- sync payload
mkdir -p "$REPO_DIR"/{hermes,host/bin,host/systemd,host/etc,host/ai-stack,host/backups}

# Hermes own state
install -m 600 -D "$HERMES_HOME/config.yaml" "$REPO_DIR/hermes/config.yaml"
[[ -f "$HERMES_HOME/SOUL.md" ]] && install -m 600 -D "$HERMES_HOME/SOUL.md" "$REPO_DIR/hermes/SOUL.md"

rsync -a --delete --exclude '*.lock' \
    "$HERMES_HOME/memories/" "$REPO_DIR/hermes/memories/"

# skills: skip the hub clone/caches, keep authored + curated skills
rsync -a --delete \
    --exclude '.hub/' \
    --exclude '.locks/' \
    --exclude '.curator_backups/' \
    --exclude '.curator_ledger.jsonl' \
    --exclude '.usage.json' \
    --exclude '.usage.json.lock' \
    --exclude '*.lock' \
    --exclude '__pycache__/' \
    "$HERMES_HOME/skills/" "$REPO_DIR/hermes/skills/"

rsync -a --delete "$HERMES_HOME/plugins/" "$REPO_DIR/hermes/plugins/"
rsync -a --delete "$HERMES_HOME/hooks/"   "$REPO_DIR/hermes/hooks/"
rsync -a --delete --exclude '__pycache__/' \
    "$HERMES_HOME/scripts/" "$REPO_DIR/hermes/scripts/"

mkdir -p "$REPO_DIR/hermes/cron"
[[ -f "$HERMES_HOME/cron/jobs.json" ]] && install -m 600 -D "$HERMES_HOME/cron/jobs.json" "$REPO_DIR/hermes/cron/jobs.json"

# host-side runbooks / ops scripts
HOST_BIN=(
    docker-incident-detector.py
    docker-incident-agent.sh
    docker-detector-liveness.sh
    hermes-boot-notify.sh
    hermes-state-backup.sh
    hermes-state-watch.sh
    searxng-quality-check.py
    litellm-watchdog.py
    litellm-spend-retention.sh
    webui-compact-images.sh
    webui-generated-images-retention.sh
    webui-data-backup.py
    webui-data-restore.py
    owui-signup-approver.py
    tor-rotate.sh
    fw-guard-postgres.sh
)
for f in "${HOST_BIN[@]}"; do
    [[ -f "/usr/local/bin/$f" ]] && install -m 700 -D "/usr/local/bin/$f" "$REPO_DIR/host/bin/$f"
done

HOST_UNITS=(
    hermes-state-backup.service hermes-state-backup.timer hermes-state-watch.service
    searxng-quality.service searxng-quality.timer
    hermes-boot-notify.service
    docker-autorecover.service docker-autorecover.timer
    docker-detector-liveness.service docker-detector-liveness.timer
    litellm-watchdog.service litellm-watchdog.timer
    litellm-spend-retention.service litellm-spend-retention.timer
    postgres-backup.service postgres-backup.timer postgres-backup-notify.service postgres-backup-notify.timer
    webui-data-backup.service webui-data-backup.timer
)
for f in "${HOST_UNITS[@]}"; do
    [[ -f "/etc/systemd/system/$f" ]] && install -m 644 -D "/etc/systemd/system/$f" "$REPO_DIR/host/systemd/$f"
done

HOST_ETC=(docker-autorecover.conf hermes-boot-expected-containers.txt)
for f in "${HOST_ETC[@]}"; do
    [[ -f "/etc/$f" ]] && install -m 644 -D "/etc/$f" "$REPO_DIR/host/etc/$f"
done

[[ -f /etc/logrotate.d/hermes-state-backup ]] && \
    install -m 644 -D /etc/logrotate.d/hermes-state-backup "$REPO_DIR/host/etc/logrotate.d-hermes-state-backup"

[[ -f /home/ai_service/ai-stack/ai/backup-databases.sh ]] && \
    install -m 644 -D /home/ai_service/ai-stack/ai/backup-databases.sh "$REPO_DIR/host/ai-stack/backup-databases.sh"

# runbook-и стека: единственный документ «что делать в 3 часа ночи» — держим в бэкапе
for rb in runbook-containers.md runbook.md; do
    [[ -f "/home/ai_service/ai-stack/$rb" ]] && \
        install -m 644 -D "/home/ai_service/ai-stack/$rb" "$REPO_DIR/host/ai-stack/$rb"
done

# Реестр file_id дампов, отправленных в Telegram. Бот не читает свою историю чата, поэтому
# без этого файла install.py --from-bot не может скачать последний бэкап на новом сервере.
[[ -f /home/ai_service/ai-stack/backups/postgres/.tg-registry.json ]] && \
    install -m 644 -D /home/ai_service/ai-stack/backups/postgres/.tg-registry.json \
        "$REPO_DIR/host/backups/tg-registry.json"

# README (first run only — afterwards it is user-editable)
if [[ ! -f "$REPO_DIR/README.md" ]]; then
    cat > "$REPO_DIR/README.md" <<'EOF'
# Hermes agent state (private)

Automatic snapshot of everything that differs from a stock Hermes install on the VPS.
Written by `/usr/local/bin/hermes-state-backup.sh`, triggered by
`hermes-state-watch.service` (inotify on config/skills/memories/units) a couple of
minutes after something actually changed — not on a schedule. Committed only when something changed.

## Layout

| path | content |
|---|---|
| `hermes/config.yaml` | agent configuration (secrets referenced via `${ENV_VAR}`, not inline) |
| `hermes/SOUL.md` | persona / operating instructions |
| `hermes/memories/` | `MEMORY.md` (agent notes) and `USER.md` (user profile) |
| `hermes/skills/` | authored/curated skills (hub clone and caches excluded) |
| `hermes/cron/jobs.json` | scheduled cron job definitions |
| `hermes/plugins/`, `hermes/hooks/`, `hermes/scripts/` | extensions and helper scripts |
| `host/bin/` | ops scripts from `/usr/local/bin` (incident detector, watchdogs, retention…) |
| `host/systemd/` | unit files from `/etc/systemd/system` |
| `host/etc/` | `docker-autorecover.conf`, expected-containers list, logrotate rule |
| `host/ai-stack/` | DB backup script of the AI stack |
| `MANIFEST.sha256` | checksums of every snapshot file |

## Not included (by design)

`/root/.hermes/.env`, `/home/ai_service/ai-stack/ai/.env`, `state.db`, `sessions/`,
`logs/`, `cache/`, `installs/`, `hermes-agent/` (the install itself), `.hermes/skills/.hub`.
API keys, tokens and session history never leave the host through this repo.

## Restore (sketch)

1. Install Hermes, stop the gateway (`systemctl --user stop hermes-gateway`).
2. Copy `hermes/*` over `/root/.hermes/`, `host/bin/*` to `/usr/local/bin`,
   `host/systemd/*` to `/etc/systemd/system`, `host/etc/*` to `/etc/`, then
   `systemctl daemon-reload && systemctl --user daemon-reload`.
3. Re-create `.hermes/.env` / `ai-stack/ai/.env` with the real secrets, then
   `systemctl --user restart hermes-gateway`.
EOF
    log "wrote README.md"
fi

# integrity manifest (paths relative to repo root)
(
    cd "$REPO_DIR"
    find . -path ./.git -prune -o -type f ! -name 'MANIFEST.sha256' -print0 \
        | sort -z | xargs -0 sha256sum > MANIFEST.sha256
) || die "manifest generation failed"

# ---------------------------------------------------------------- secret guard
SECRET_RE='(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|xoxb-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|[0-9]{9,10}:AA[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
HITS="$(grep -rInE "$SECRET_RE" "$REPO_DIR" --exclude-dir=.git 2>/dev/null \
        | grep -vE '(xxx|XXX|\.\.\.|YOUR_|<[A-Z_]{3,}>|REDACTED|example|placeholder)' || true)"
if [[ -n "$HITS" ]]; then
    log "SECRET-LIKE STRINGS FOUND — commit aborted:"
    echo "$HITS" | head -n 5 | cut -c1-200 | tee -a "$LOG_FILE" >&2
    die "$(echo "$HITS" | wc -l) secret-like string(s) in snapshot"
fi

# ---------------------------------------------------------------- commit + push
git -C "$REPO_DIR" add -A

push_remote() {
    if GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 \
            git -C "$REPO_DIR" push -q origin main 2>>"$LOG_FILE"; then
        log "✓ pushed $(git -C "$REPO_DIR" rev-parse --short HEAD) to $GIT_REMOTE"
        return 0
    fi
    return 1
}

remote_head() {
    GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 \
        git -C "$REPO_DIR" ls-remote origin refs/heads/main 2>/dev/null | awk '{print $1}' | head -n1
}

# Алерт о неудачном пуше — не чаще раза в 30 минут: вотчер ретраит каждые
# 60→300 с, иначе владелец получает десяток одинаковых сообщений за аварию.
push_fail() {
    local msg="$1" stamp="$STATE_DIR/.last-push-alert" now last=0
    now=$(date +%s)
    [[ -f "$stamp" ]] && last=$(cat "$stamp" 2>/dev/null || echo 0)
    [[ -z "$last" ]] && last=0
    log "ERROR: $msg"
    if (( now - last >= 1800 )); then
        printf '%s\n' "$now" >"$stamp"
        notify_fail "$msg"
    else
        log "push-failure alert throttled ($(( (now - last) / 60 )) min since last alert)"
    fi
    exit 1
}

if git -C "$REPO_DIR" diff --cached --quiet; then
    # Новых правок нет, но предыдущий push мог не пройти: локальный HEAD
    # опережает origin. Догоняем, иначе коммит висит на хосте навсегда.
    LOCAL_HEAD="$(git -C "$REPO_DIR" rev-parse HEAD)"
    if [[ "$(remote_head)" != "$LOCAL_HEAD" ]]; then
        log "local HEAD $LOCAL_HEAD missing on remote — catch-up push"
        push_remote || push_fail "git push to $GIT_REMOTE failed (catch-up)"
    else
        log "no changes since last snapshot — nothing to commit"
    fi
    exit 0
fi

STAT="$(git -C "$REPO_DIR" diff --cached --shortstat | sed -E 's/^ +//')"
FILES="$(git -C "$REPO_DIR" diff --cached --name-only | wc -l)"
git -C "$REPO_DIR" commit -q -m "state snapshot $(date -u '+%Y-%m-%d %H:%M UTC')" \
    -m "$STAT" -m "$FILES file(s) changed" \
    || die "git commit failed"
COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
log "✓ committed $COMMIT ($STAT)"

push_remote || push_fail "git push to $GIT_REMOTE failed (commit $COMMIT stays local, watcher retries)"

git -C "$REPO_DIR" gc --auto -q >/dev/null 2>&1 || true
log "=== snapshot complete: $(du -sh "$REPO_DIR" | cut -f1) repo, $(git -C "$REPO_DIR" rev-list --count HEAD) commits ==="
