#!/usr/bin/env bash
# Move base64 images out of the Open WebUI chat tables into the file storage
# (/app/backend/data/uploads -> host /home/ai_service/webui_data/uploads).
# Idempotent: a run with nothing to migrate exits quietly. Reclaims disk with
# VACUUM FULL only when something was actually moved.
set -uo pipefail

LOG=/var/log/webui-compact-images.log
SCRIPT=/usr/local/lib/webui-compact/compact_images.py
PG_DB=webui_db
TS=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

log() { printf '%s %s\n' "$TS" "$*" >> "$LOG"; }

if ! docker inspect -f '{{.State.Running}}' open-webui >/dev/null 2>&1; then
    log "ERROR: open-webui container is not running, skipped"
    exit 1
fi

out=$(docker cp "$SCRIPT" open-webui:/tmp/compact_images.py 2>&1 &&
      docker exec open-webui python -c "
import os
os.environ['DSN'] = os.environ['DATABASE_URL']
exec(open('/tmp/compact_images.py').read())
" 2>&1)
rc=$?

if [ "$rc" -ne 0 ]; then
    log "ERROR: migration failed (rc=$rc): $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
    exit 1
fi

if ! printf '%s' "$out" | grep -q '^committed$'; then
    log "ERROR: unexpected output: $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
    exit 1
fi

images=$(printf '%s' "$out" | sed -n 's/^images: \([0-9]\+\),.*/\1/p')
images=${images:-0}

if [ "$images" -eq 0 ]; then
    exit 0
fi

summary=$(printf '%s' "$out" | sed -n 's/^images: /images: /p')
log "$summary; running VACUUM FULL"
sudo -u postgres psql -d "$PG_DB" -q -c "vacuum (full, analyze) chat" >>"$LOG" 2>&1
sudo -u postgres psql -d "$PG_DB" -q -c "vacuum (full, analyze) chat_message" >>"$LOG" 2>&1
size=$(sudo -u postgres psql -d "$PG_DB" -Atc "select pg_size_pretty(pg_database_size('$PG_DB'))")
log "done, ${PG_DB} now $size"
