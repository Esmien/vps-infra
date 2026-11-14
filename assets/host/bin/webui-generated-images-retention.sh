#!/usr/bin/env bash
# Retention for the LEGACY shared generated_images folder
# (container /app/backend/data/generated_images -> host /home/ai_service/webui_data/generated_images).
#
# Per-user images are NOT touched here. Since the image tool writes into each user's
# workspace (`/home/ai_service/sandbox/users/<slug>/generated_images`), the owner keeps
# them for the long haul (re-extraction years later): they are in `PROTECTED` in
# sandbox-provisioner.py, so the quota LRU skips them, and they reach the weekly offsite
# archive. Do not "fix" this by adding them back to the deletion loop.
#
# The legacy folder is frozen (nothing new lands there) and stays a pure cache of the
# admin's own generations; deleting old files here does not break images in old chats —
# chat history references `/api/v1/files/<uuid>/content`. Default: keep RETENTION_DAYS = 14.
#
# Usage: webui-generated-images-retention.sh [--dry-run]
set -uo pipefail

DIR=${WEBUI_GENERATED_IMAGES_DIR:-/home/ai_service/webui_data/generated_images}
LOG=/var/log/webui-generated-images-retention.log
RETENTION_DAYS=14
TS=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { printf '%s %s\n' "$TS" "$*" >> "$LOG"; }

if [ ! -d "$DIR" ]; then
    log "ERROR: $DIR not found, skipped"
    exit 1
fi

# Only regular image files directly in the folder, older than RETENTION_DAYS by mtime.
mapfile -d '' OLD < <(find "$DIR" -maxdepth 1 -type f \
    \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' -o -iname '*.gif' \) \
    -mtime "+$RETENTION_DAYS" -print0)

count=${#OLD[@]}
bytes=0
for f in "${OLD[@]:-}"; do
    [ -n "$f" ] || continue
    bytes=$(( bytes + $(stat -c %s "$f" 2>/dev/null || echo 0) ))
done

if [ "$DRY" -eq 1 ]; then
    printf 'would delete: %d files, %.1f MiB from %s\n' \
        "$count" "$(awk -v b="$bytes" 'BEGIN{print b/1048576}')" "$DIR"
    exit 0
fi

if [ "$count" -eq 0 ]; then
    exit 0
fi

for f in "${OLD[@]}"; do
    rm -f -- "$f"
done

total=$(find "$DIR" -maxdepth 1 -type f | wc -l)
log "deleted=$count files ($(awk -v b="$bytes" 'BEGIN{printf "%.1f", b/1048576}') MiB, retention ${RETENTION_DAYS}d), ${total} files left"
