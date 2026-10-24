#!/usr/bin/env bash
# Nightly restart of open-terminal: its glibc heap keeps a high-water floor that is
# never returned to the OS (see ai-stack skill). A restart resets RSS to ~85 MB.
# Pauses the docker incident detector for the duration so the restart does not
# fire an incident agent.
set -uo pipefail

CONTAINER=open-terminal
PAUSE=/var/lib/docker-autorecover/pause
LOG=/var/log/open-terminal-restart.log
HEALTH_TIMEOUT=120

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

touch "$PAUSE"
cleanup() { rm -f "$PAUSE"; }
trap cleanup EXIT

before=$(docker inspect "$CONTAINER" --format '{{.State.Health.Status}} rss_unknown')
before_rss=$(awk '/VmRSS/{print $2}' "/proc/$(pgrep -f 'open-terminal run --cwd /mnt/host' | head -n1)/status" 2>/dev/null || echo '?')

if ! docker restart "$CONTAINER" >/dev/null 2>&1; then
    log "ERROR: docker restart $CONTAINER failed"
    exit 1
fi

deadline=$((SECONDS + HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
    status=$(docker inspect "$CONTAINER" --format '{{.State.Health.Status}}' 2>/dev/null)
    [[ "$status" == "healthy" ]] && break
    sleep 3
done

if [[ "${status:-}" != "healthy" ]]; then
    log "ERROR: $CONTAINER not healthy after ${HEALTH_TIMEOUT}s (state=${status:-unknown})"
    exit 1
fi

after_rss=$(awk '/VmRSS/{print $2}' "/proc/$(pgrep -f 'open-terminal run --cwd /mnt/host' | head -n1)/status" 2>/dev/null || echo '?')
log "OK: restarted $CONTAINER, health=healthy, rss_before=${before_rss}kB rss_after=${after_rss}kB"
exit 0
