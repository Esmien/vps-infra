#!/bin/bash
# Пробуждает Hermes-агента на разбор инцидента с контейнером.
# Вызывается детектором через systemd-run: docker-incident-agent.sh <incident_dir>
# Результат (отчёт агента) уходит владельцу в Telegram.
set -uo pipefail

HERMES=/root/.hermes/hermes-agent/.hermes/bin/hermes
LOG=/var/log/docker-autorecover.log
DIR=${1:-}
[ -d "$DIR" ] || { echo "incident dir missing: $DIR"; exit 2; }

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

read -r NAME REASON < <(python3 - "$DIR/incident.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("container", "?"), d.get("reason", "?"))
PY
)
log "AGENT START $NAME reason=$REASON dir=$DIR"

cat >"$DIR/prompt.txt" <<EOF
Ты проснулся автоматически: дежурный разбор инцидента с контейнером на этом VPS.
Владелец не дежурит — не задавай вопросов, работай и отчитайся.

ИНЦИДЕНТ: контейнер ${NAME}, триггер детектора: ${REASON}
Доказательства: ${DIR}
  incident.json       — статус, health, RestartCount, exit code, OOMKilled, политика restart, образ
  inspect.json        — полный inspect (лимиты памяти, бинды, env-ключи без значений)
  logs.txt            — последние 300 строк логов с таймстампами
  oom.txt             — OOM-события ядра
  disk.txt            — df + docker system df
  host.txt            — postgresql/nginx/docker, память, упавшие юниты
  docker-journal.txt  — строки journalctl -u docker про этот контейнер

ЗАДАЧА: найти ПРИЧИНУ, а не симптом.
1. Сначала факты. OOMKilled=true — смотри лимиты и рост памяти; exit code 1 сразу после старта — конфиг/переменные/миграции; 137 без OOM — SIGKILL (таймаут stop_grace_period или OOM cgroup); 143 — SIGTERM (кто-то остановил).
2. Прочитай /home/ai_service/ai-stack/runbook-containers.md (§1–§5, §8 — известный шум, §9 — автоподъём) и при работе с compose — skill_view(name='docker-compose-traps').
3. Отдели известный шум (§8) от реальной поломки. Если контейнер уже поднялся сам — укажи причину и подтверди, что он здоров.
4. Чинить можно только безопасным: docker start / docker restart; правка compose с бэкапом *.bak.<ts> и recreate ОДНОГО сервиса; удаление собственных временных файлов. Перед ручными действиями с контейнерами: touch /var/lib/docker-autorecover/pause, после проверки — снять файл.
5. БЕЗ подтверждения владельца запрещено: rm/rm -rf, docker rm/rmi, docker system prune, docker compose down -v, восстановление БД из дампа, правка .env, пересоздание контейнеров вне инцидента. Если починка требует такого — не делай, объясни в отчёте.
6. Проверь результат по §5 ранбука: health, реальный endpoint, отсутствие ошибок в логах за 3 минуты.

ФОРМАТ: последний блок ответа — ровно так, его перешлют владельцу:
ОТЧЁТ:
причина: <одна-две строки>
что сделал: <одна-две строки>
состояние: работает | требует человека — <что именно и почему>

По-русски, сжато, без пересказа команд и без секретов (ключи, токены, пароли, содержимое .env — не выводить).
EOF

MODEL_ARGS=()
case "$NAME" in
  litellm|litellm-redis) MODEL_ARGS=(-m emergency) ;;  # шлюз может быть мёртв — думаем через аварийный маршрут
esac

timeout 1500 "$HERMES" chat --query-file "$DIR/prompt.txt" --oneshot \
    --run-budget 1200 --max-turns 80 -Q "${MODEL_ARGS[@]}" >"$DIR/agent.log" 2>&1
RC=$?
log "AGENT DONE $NAME rc=$RC"

python3 - "$DIR" "$NAME" "$REASON" "$RC" <<'PY'
import json, os, sys, urllib.request

d, name, reason, rc = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
log_path = os.path.join(d, "agent.log")
text = ""
try:
    text = open(log_path, encoding="utf-8", errors="replace").read()
except OSError:
    pass

report = ""
marker = "ОТЧЁТ:"
if marker in text:
    report = text.rsplit(marker, 1)[1].strip()
if not report:
    tail = [l for l in text.strip().splitlines() if l.strip()][-12:]
    report = ("агент не выдал отчёт (rc=%s), хвост вывода:\n" % rc) + "\n".join(tail)

report = report[:3000]
body = (f"🚨 <b>{name}</b> — разбор ({reason})\n\n"
        f"<pre>{report.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</pre>")

def load_env() -> None:
    p = "/home/ai_service/notifications/.env"
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()
token, chat = os.environ.get("BOT_TOKEN", ""), os.environ.get("BOT_OWNER", "")
if token and chat:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": body, "parse_mode": "HTML"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("report sent ok=", json.loads(r.read().decode()).get("ok"))
    except Exception as exc:  # noqa: BLE001
        print("report send failed:", exc)
else:
    print("no token/owner — report not sent")
PY

log "AGENT REPORT SENT $NAME"
