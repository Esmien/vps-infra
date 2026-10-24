#!/bin/bash
# Send a Telegram message after boot, once the Hermes gateway and the
# Docker containers are up. Installed by hermes-boot-notify.service.
set -u

export HERMES_HOME=/root/.hermes
HERMES=/root/.hermes/hermes-agent/.hermes/bin/hermes
LOG=/var/log/hermes-boot-notify.log
EXPECTED=/etc/hermes-boot-expected-containers.txt

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

log "boot-notify start"

# --- 1. Wait for DNS/network (max 60s) -------------------------------------
for _ in $(seq 1 30); do
    getent hosts api.telegram.org >/dev/null 2>&1 && break
    sleep 2
done

# --- 2. Wait for the Hermes gateway process (max 180s) ---------------------
gw_ok=0
for _ in $(seq 1 90); do
    if pgrep -f "gateway run" >/dev/null 2>&1; then
        gw_ok=1
        break
    fi
    sleep 2
done
[ "$gw_ok" -eq 1 ] || log "WARN: gateway not detected after 180s"

# --- 3. Wait for Docker + expected containers (max 240s) -------------------
docker_ok=0
if command -v docker >/dev/null 2>&1; then
    for _ in $(seq 1 60); do
        docker info >/dev/null 2>&1 && { docker_ok=1; break; }
        sleep 2
    done
    if [ "$docker_ok" -eq 1 ] && [ -f "$EXPECTED" ]; then
        for _ in $(seq 1 120); do
            missing=0
            while read -r c; do
                [ -z "$c" ] && continue
                st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
                [ "$st" = "running" ] || missing=$((missing + 1))
            done <"$EXPECTED"
            [ "$missing" -eq 0 ] && break
            sleep 2
        done
    fi
fi

# --- 4. Build the container report ----------------------------------------
total=0
up=0
down_list=""
if [ "$docker_ok" -eq 1 ] && [ -f "$EXPECTED" ]; then
    while read -r c; do
        [ -z "$c" ] && continue
        total=$((total + 1))
        st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
        if [ "$st" = "running" ]; then
            up=$((up + 1))
        else
            down_list="${down_list}${c} (${st}), "
        fi
    done <"$EXPECTED"
fi

if [ "$docker_ok" -ne 1 ]; then
    docker_line="⚠️ Docker daemon не отвечает"
elif [ "$total" -eq 0 ]; then
    docker_line="⚠️ Список контейнеров не найден"
elif [ "$up" -eq "$total" ]; then
    docker_line="✅ ${up}/${total} контейнеров запущено"
else
    docker_line="⚠️ ${up}/${total} контейнеров — не поднялись: ${down_list%, }"
fi

if [ "$gw_ok" -eq 1 ]; then
    gw_line="✅ запущен"
else
    gw_line="⚠️ процесс не найден"
fi

# --- 5. Send ---------------------------------------------------------------
sleep 5

BOOT_UTC=$(uptime -s)
BOOT_EPOCH=$(date -d "$BOOT_UTC" +%s 2>/dev/null || echo 0)
DELAY=$(( $(date +%s) - BOOT_EPOCH ))

MSG="🔄 *Сервер перезагружен*

Загрузка: ${BOOT_UTC} UTC
Гейтвей: ${gw_line} (~${DELAY} сек)
Docker: ${docker_line}

Hermes на связи и готов отвечать."

if printf '%s\n' "$MSG" | "$HERMES" send -t telegram -q >>"$LOG" 2>&1; then
    log "notification sent | gw=${gw_ok} docker=${docker_ok} up=${up}/${total} down=[${down_list%, }]"
else
    log "ERROR: hermes send failed (exit $?)"
    exit 1
fi
