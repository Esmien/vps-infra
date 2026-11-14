#!/usr/bin/env bash
# Retention for generated_images folders.
#
# Two places hold generated images:
#   1. the legacy shared folder
#      (container /app/backend/data/generated_images -> host /home/ai_service/webui_data/generated_images);
#   2. per-user sandbox workspaces `/home/ai_service/sandbox/users/<slug>/generated_images`
#      — since the image tool writes into the user's workspace, new images only land here.
#
# Images are ordinary user data: they age out like everything else, and the sandbox quota
# LRU may evict them (they are deliberately NOT in `PROTECTED` in sandbox-provisioner.py).
# The long-term copy lives in the weekly offsite archive, not on this disk.
#
# These are on-disk copies; chat history references images through
# /api/v1/files/<uuid>/content, so deleting old files here does NOT break images in old
# chats. It only removes the disk fallback the image tool uses for reference resolution,
# which only matters for very recent generations. Default: keep RETENTION_DAYS = 14.
#
# The weekly offsite backup (webui-data-backup.timer) runs before this timer, so an image
# is archived at least once before it becomes eligible for deletion.
#
# Usage: webui-generated-images-retention.sh [--dry-run]
set -uo pipefail

DIR=${WEBUI_GENERATED_IMAGES_DIR:-/home/ai_service/webui_data/generated_images}
SANDBOX_USERS=${SANDBOX_USERS_DIR:-/home/ai_service/sandbox/users}
LOG=/var/log/webui-generated-images-retention.log
RETENTION_DAYS=14
TS=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { printf '%s %s\n' "$TS" "$*" >> "$LOG"; }

dirs=()
[ -d "$DIR" ] && dirs+=("$DIR")
for d in "$SANDBOX_USERS"/*/generated_images; do
    [ -d "$d" ] && dirs+=("$d")
done

if [ ${#dirs[@]} -eq 0 ]; then
    log "ERROR: neither $DIR nor per-user generated_images found, skipped"
    exit 1
fi

total_count=0
total_bytes=0
total_left=0
for d in "${dirs[@]}"; do
    # Only regular image files directly in the folder, older than RETENTION_DAYS by mtime.
    mapfile -d '' OLD < <(find "$d" -maxdepth 1 -type f \
        \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' -o -iname '*.gif' \) \
        -mtime "+$RETENTION_DAYS" -print0)

    count=${#OLD[@]}
    bytes=0
    for f in "${OLD[@]:-}"; do
        [ -n "$f" ] || continue
        bytes=$(( bytes + $(stat -c %s "$f" 2>/dev/null || echo 0) ))
    done
    left=$(find "$d" -maxdepth 1 -type f | wc -l)
    total_count=$(( total_count + count ))
    total_bytes=$(( total_bytes + bytes ))
    total_left=$(( total_left + left ))

    if [ "$DRY" -eq 1 ]; then
        printf '%s would delete: %d files, %.1f MiB\n' \
            "$d" "$count" "$(awk -v b="$bytes" 'BEGIN{print b/1048576}')"
        continue
    fi
    [ "$count" -eq 0 ] && continue
    for f in "${OLD[@]}"; do
        rm -f -- "$f"
    done
    log "$(basename "$(dirname "$d")")/generated_images: deleted=$count files ($(awk -v b="$bytes" 'BEGIN{printf "%.1f", b/1048576}') MiB, ${left} left)"
done

if [ "$DRY" -eq 1 ]; then
    printf 'TOTAL would delete: %d files, %.1f MiB across %d folders (%d files stay)\n' \
        "$total_count" "$(awk -v b="$total_bytes" 'BEGIN{print b/1048576}')" "${#dirs[@]}" "$total_left"
    exit 0
fi

[ "$total_count" -eq 0 ] && exit 0
log "total deleted=$total_count files ($(awk -v b="$total_bytes" 'BEGIN{printf "%.1f", b/1048576}') MiB, retention ${RETENTION_DAYS}d, folders=${#dirs[@]}), ${total_left} files left"
