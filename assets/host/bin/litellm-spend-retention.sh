#!/usr/bin/env bash
# Retention for LiteLLM detail spend logs.
# Deletes rows of LiteLLM_SpendLogs older than RETENTION_DAYS.
# Daily aggregates (LiteLLM_DailyUserSpend / DailyTeamSpend / DailyTagSpend /
# DailyModelUsage) are NEVER touched - they are the long-term cost history.
# Plain VACUUM every run; VACUUM FULL only when dead tuples exceed
# VACUUM_FULL_DEAD_RATIO of live rows (it takes an ACCESS EXCLUSIVE lock).
set -uo pipefail

LOG=/var/log/litellm-spend-retention.log
PG_DB=litellm_db
RETENTION_DAYS=30
VACUUM_FULL_DEAD_RATIO=0.30
TS=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

log() { printf '%s %s\n' "$TS" "$*" >> "$LOG"; }

deleted=$(sudo -u postgres psql -d "$PG_DB" -Atc \
    "with d as (delete from \"LiteLLM_SpendLogs\" where \"startTime\" < now() - interval '$RETENTION_DAYS days' returning 1) select count(*) from d")
rc=$?
if [ "$rc" -ne 0 ]; then
    log "ERROR: delete failed (rc=$rc)"
    exit 1
fi

sudo -u postgres psql -d "$PG_DB" -q -c "vacuum (analyze) \"LiteLLM_SpendLogs\"" >>"$LOG" 2>&1

stats=$(sudo -u postgres psql -d "$PG_DB" -Atc \
    "select n_dead_tup, n_live_tup from pg_stat_user_tables where relname = 'LiteLLM_SpendLogs'")
dead=$(printf '%s' "$stats" | cut -d'|' -f1)
live=$(printf '%s' "$stats" | cut -d'|' -f2)

full="skipped"
if [ "${live:-0}" -gt 0 ] && [ "${dead:-0}" -gt 0 ]; then
    if awk -v d="$dead" -v l="$live" -v r="$VACUUM_FULL_DEAD_RATIO" 'BEGIN{exit !(d/l > r)}'; then
        sudo -u postgres psql -d "$PG_DB" -q -c "vacuum (full, analyze) \"LiteLLM_SpendLogs\"" >>"$LOG" 2>&1
        full="ran"
    fi
fi

size=$(sudo -u postgres psql -d "$PG_DB" -Atc \
    "select pg_size_pretty(pg_total_relation_size('\"LiteLLM_SpendLogs\"')::bigint)")

log "deleted=$deleted rows (retention ${RETENTION_DAYS}d), vacuum_full=$full, table now $size"
