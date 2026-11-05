#!/bin/bash
# Пробуждает Hermes-агента на разбор инцидента с LiteLLM-шлюзом.
# Вызывается watchdog'ом через systemd-run: litellm-incident-agent.sh <incident_dir>
# Отчёт агента уходит владельцу в Telegram; сам факт тревоги watchdog уже отправил.
set -uo pipefail

HERMES=/root/.hermes/hermes-agent/.hermes/bin/hermes
LOG=/var/log/litellm-watchdog.log
DIR=${1:-}
[ -d "$DIR" ] || { echo "incident dir missing: $DIR"; exit 2; }
[ -f "$DIR/alert.json" ] || { echo "alert.json missing in $DIR"; exit 2; }

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

read -r REASON DETAIL < <(python3 - "$DIR/alert.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("reason", "?"), (d.get("detail") or "").replace("\n", " ")[:300])
PY
)
log "AGENT START litellm reason=$REASON dir=$DIR"

cat >"$DIR/prompt.txt" <<EOF
Ты проснулся автоматически: дежурный разбор инцидента с LiteLLM-шлюзом на этом VPS.
Владелец не дежурит — не задавай вопросов, работай и отчитайся.

ИНЦИДЕНТ: LiteLLM, триггер сторожа: ${REASON}
Детали: ${DETAIL}
Улики: ${DIR}
  alert.json    — триггер, время UTC, URL пробы, окно анализа
  health.txt    — коды /health/liveliness, /health/readiness, /health
  containers.txt— статус litellm, restart count, exit code, OOMKilled, restart policy
  logs.txt      — последние 200 строк логов шлюза
  spendlog.txt  — последние вызовы за окно (группа / call_type / статус)
  failures.txt  — все неуспешные вызовы за 24 часа с кодом и началом ответа
  resource.txt  — память, диск, упавшие юниты

ПЕРВЫМ ДЕЛОМ прочитай ранбук: skill_view(name='litellm-fallout-triage').
Он объясняет, что уже известно про ложные срабатывания и как отличить их от реального отказа.

ЗАДАЧА: назвать ПРИЧИНУ, а не симптом.
1. Сначала факты из улик: жив ли процесс, отвечает ли liveliness, что в failures.txt
   (провайдер, группа модели, текст ошибки), не OOM ли это.
2. Отдели известный шум от поломки. Помни: админские endpoint'ы прокси
   (/v2/model/info, /get/user_banner, /model_group/info, /team/list,
   /sso/get/ui_settings, /get/latest_release_info) в спенд-логах выглядят как отказы,
   но модель не вызывают — сторож их теперь отбрасывает, и если тревога всё же пришла,
   значит падают настоящие acompletion/aembedding/aresponses-вызовы.
3. Проверь цепочку: ключи группы моделей в LiteLLM → доступность провайдера изнутри контейнера
   (curl из litellm к api провайдера) → квоты/лимиты провайдера → сеть/диск/память хоста.
4. Чинить можно только безопасное: docker restart litellm; удаление своих временных файлов.
   Перед ручными действиями: touch /var/lib/docker-autorecover/pause, после проверки — снять.
5. БЕЗ подтверждения владельца запрещено: rm, docker rm/rmi, prune, down -v,
   правка .env и config.yaml LiteLLM, восстановление БД, пересоздание контейнеров.
   Если починка требует такого — не делай, объясни в отчёте, что именно нужно и почему.
6. Проверь результат как пользователь: /health/liveliness = 200, реальный вызов модели
   через шлюз проходит, в логах за 3 минуты нет новых ошибок.

ФОРМАТ: последний блок ответа — ровно так, его перешлют владельцу:
ОТЧЁТ:
причина: <одна-две строки, механизм>
что сделал: <одна-две строки>
состояние: работает | требует человека — <что именно и почему>

По-русски, сжато, без пересказа команд и без секретов (ключи, токены, пароли, содержимое .env — не выводить).
EOF

# Шлюз может быть мёртв — думаем через аварийный маршрут, он не зависит от LiteLLM.
timeout 1500 "$HERMES" chat --query-file "$DIR/prompt.txt" --oneshot \
    --run-budget 1200 --max-turns 80 -Q -m emergency >"$DIR/agent.log" 2>&1
RC=$?
log "AGENT DONE litellm rc=$RC"

python3 - "$DIR" "$REASON" "$RC" <<'PY'
import json, os, sys, urllib.request

d, reason, rc = sys.argv[1], sys.argv[2], sys.argv[3]
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
body = (f"🛠 <b>LiteLLM</b> — разбор ({reason})\n\n"
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
token = os.environ.get("BOT_TOKEN", "")
chat = os.environ.get("BOT_TARGET_CHAT") or os.environ.get("BOT_OWNER", "")
thread = os.environ.get("BOT_TARGET_THREAD", "")
if token and chat:
    payload = {"chat_id": chat, "text": body, "parse_mode": "HTML"}
    if thread:
        payload["message_thread_id"] = int(thread) if thread.isdigit() else thread
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("report sent ok=", json.loads(r.read().decode()).get("ok"))
    except Exception as exc:  # noqa: BLE001
        print("report send failed:", exc)
else:
    print("no token/owner — report not sent")
PY

log "AGENT REPORT SENT litellm"
