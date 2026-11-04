# REPORT.md — что проверять после развёртывания

Этот файл — ранбук для агента (Hermes) и для человека. Он описывает, как убедиться, что сервер
развернулся и работает так, как задумано, и как проверить, что данные действительно восстанавливаются.

Его можно передать агенту тремя способами:

- **автоматически** — установщик ставит одноразовый таймер `hermes-postcheck.timer`: через 15 минут
  после установки Hermes сам читает этот файл, выполняет раздел 1 и присылает отчёт в Telegram;
- **вручную, целиком** — «прочитай `/opt/vps-infra/REPORT.md` и выполни раздел 1»;
- **вручную, по одному разделу** — «проверь восстановление» (раздел 2), «проверь бэкапы» (раздел 3),
  «проверь VPN» (раздел 4).

Агенту: разделы 1–4 — **только чтение**. Ничего не перезапускай, не удаляй, не правь конфиги и `.env`
без отдельного разрешения владельца. Если проверка требует изменения состояния — остановись,
сформулируй, что именно нужно, и спроси.

Машинная часть: `python3 install.py check` в каталоге установщика прогоняет preflight + health
(контейнеры, HTTP, Postgres, Redis) и печатает результат сам. Разделы ниже добавляют то, чего
установщик знать не может: доставку уведомлений, свежесть и восстановимость копий, TLS, периметр,
Hermes и VPN.

---

## 0. Быстрый вердикт (1 минута)

Пять команд, по которым уже видно, живой ли сервер:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | sort
curl -s -o /dev/null -w 'litellm %{http_code}\n' http://127.0.0.1:4000/health/liveliness
curl -s -o /dev/null -w 'webui %{http_code}\n'   http://127.0.0.1:8080/health
df -h / | tail -1
free -h | sed -n '1,3p'
```

Ожидаемо: все контейнеры стека `Up ... (healthy)`; `litellm 200`, `webui 200`; диск занят меньше 80 %;
swap занят меньше половины. Если это выполняется — сервер здоров, дальше проверяем то, что не видно
глазами: копии, TLS, уведомления.

---

## 1. Пост-проверка после развёртывания

Десять блоков. Каждый: что проверяем, чем, что считается нормой, что делать при отклонении.

### 1.1 Контейнеры

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect litellm open-webui open-terminal open-terminal-sandbox searxng litellm-redis \
  --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}'
```

**Норма:** `litellm`, `litellm-redis`, `open-webui`, `searxng`, `open-terminal`,
`open-terminal-sandbox` — `running` + `healthy`, `restarts=0`.
**Плохо:** `restarting`, `exited`, `unhealthy`, `restarts` растёт — это цикл падений.
**Что делать:** `docker logs <name> --tail 50`, искать причину (код выхода, OOM, отсутствующий `.env`);
не перезапускать «на всякий случай» — сначала причина.

### 1.2 HTTP-эндпоинты

```bash
for u in http://127.0.0.1:4000/health/liveliness http://127.0.0.1:8080/health http://127.0.0.1:8081/; do
  printf '%s -> ' "$u"; curl -s -o /dev/null -w '%{http_code}\n' -m 10 "$u"
done
```

**Норма:** `200` на всех трёх.
**Плохо:** `000` (нет ответа) — сервис не слушает; `5xx` — сервис поднялся, но внутри ошибка.
Для LiteLLM `5xx` почти всегда означает недоступную БД или Redis, а не модели.

### 1.3 PostgreSQL и pgvector

```bash
sudo -u postgres psql -Atc "select datname, pg_size_pretty(pg_database_size(datname)) from pg_database order by 2 desc"
sudo -u postgres psql -d webui_db  -Atc "select count(*) from pg_tables where schemaname='public'"
sudo -u postgres psql -d litellm_db -Atc "select count(*) from pg_tables where schemaname='public'"
sudo -u postgres psql -d webui_db -Atc "select extname from pg_extension where extname='vector'"
```

**Норма:** базы `webui_db` и `litellm_db` существуют; таблиц в каждой больше 5; `vector` в списке
расширений. Обе базы не пустые: `webui_db` десятки-сотни МБ, `litellm_db` сотни МБ (журнал запросов).
**Плохо:** таблиц 0 — база развёрнута пустой (не применился дамп); `vector` отсутствует — поиск по
документам не заработает.
**Что делать:** проверить, что шаг restore нашёл дамп (`/home/ai_service/ai-stack/backups/postgres/`),
и применить вручную; не пересоздавать базу, пока дамп не найден.

### 1.4 Redis шлюза

```bash
docker exec litellm-redis sh -c 'redis-cli -a "$LITELLM_REDIS_PASSWORD" --no-auth-warning ping'
docker exec litellm-redis sh -c 'redis-cli -a "$LITELLM_REDIS_PASSWORD" --no-auth-warning info memory | head -5'
```

**Норма:** `PONG`, память в пределах лимита 256 МБ.
**Плохо:** `NOAUTH`/`WRONGPASS` — рассинхрон пароля между `.env` и контейнером (пересоздать оба
сервиса из compose, не править пароль в одном месте).

### 1.5 Nginx и TLS

```bash
nginx -t
ls /etc/nginx/conf.d/*.conf
env -u PYTHONPATH certbot certificates | grep -E 'Certificate Name|Domains|Expiry'
```

**Норма:** `nginx -t` — `syntax is ok` / `test is successful`; в `conf.d` есть вхосты для каждого
домена стека; у каждого сертификата `VALID` и больше 30 дней до истечения.
**Плохо:** `nginx: [emerg]` — конфиг сломан, reload не делать; сертификат истекает меньше чем через
30 дней — `certbot renew --dry-run`, разобраться с ACME (A-запись, Cloudflare proxy должен быть выключен).
**Грабли:** `certbot` надо запускать с `env -u PYTHONPATH`, иначе падает на `lib.GEN_EMAIL` — это
не поломка certbot, а чужой venv в переменной окружения.

### 1.6 systemd: юниты и таймеры

```bash
systemctl --failed
systemctl is-active fail2ban fw-guard-postgres nginx docker
systemctl list-timers --all --no-pager | grep -E 'postgres-backup|postgres-backup-notify|secrets-offsite|hermes-history|webui-data-backup|litellm-watchdog|docker-autorecover|docker-detector-liveness|certbot'
```

**Норма:** `systemctl --failed` пуст; перечисленные юниты `active`; у каждого таймера в колонке `LAST`
стоит время, а не `-`.
**Плохо:** `LAST = -` значит «по расписанию ещё ни разу не запускался» — на свежей машине это нормально,
но через сутки после установки это уже сигнал.
**Что делать:** `journalctl -u <unit> -n 50 --no-pager` и смотреть `ExecStart`: таймер, который запускается,
не обязательно что-то делает.

### 1.7 Бэкапы: свежесть

```bash
ls -lt /home/ai_service/ai-stack/backups/postgres/*.dump | head -6
sudo -u postgres psql -Atc "select datname from pg_database where datname like 'verify_restore_%'"
```

**Норма:** по три последних дампа на каждую из двух баз, самый свежий не старше 26 часов; временных
баз `verify_restore_*` не осталось (значит проверка отработала и убрала за собой).
**Плохо:** дампов нет или они старше суток → `journalctl -u postgres-backup.service`; временная база
осталась → ночная проверка упала на середине, смотреть её лог.

### 1.8 Уведомления в Telegram

Токены не печатать. Проверяем, что каждый бот отвечает и что владелец получает сообщения:

```bash
set -a; . /home/ai_service/notifications/.env; set +a
curl -fsS "https://api.telegram.org/bot$BOT_TOKEN/getMe"      | python3 -c 'import sys,json;print("notify bot:",json.load(sys.stdin)["result"]["username"])'
curl -fsS "https://api.telegram.org/bot$FILE_BOT_TOKEN/getMe" | python3 -c 'import sys,json;print("file bot:",json.load(sys.stdin)["result"]["username"])'
curl -fsS -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
  -d chat_id="$BOT_OWNER" --data-urlencode 'text=postcheck: канал уведомлений жив' | head -c 120; echo
```

**Норма:** оба бота возвращают username, тестовое сообщение уходит (`"ok":true`).
**Плохо:** `404` на `getMe` — токен битый или отозван; сообщение не приходит — `chat_id` не тот.
**Грабли:** токен читается процессом при старте. После правки `.env` юнит надо перезапустить, иначе
он продолжает ходить со старым токеном и получает `404` каждые 5 секунд.

### 1.9 Hermes

```bash
systemctl is-active hermes-boot-notify.service hermes-state-watch.service
ls /root/.hermes/config.yaml /root/.hermes/skills | head
systemctl status hermes-gw-restart-once --no-pager | tail -3
```

**Норма:** состояние Hermes на месте (конфиг, скиллы, память), `hermes-state-watch` активен, гейтвей
к моделям отвечает (проверяется в 1.2 через LiteLLM).
**Плохо:** скиллы пусты — не развернулось состояние из `hermes-state` (`install.py --only hermes`).

### 1.10 Ресурсы и периметр

```bash
df -h / ; free -h ; swapon --show ; uptime
ss -tlnpH | awk '{print $4}' | sort -u
fail2ban-client status
```

**Норма:** диск занят меньше 80 %; swap занят меньше половины; load average заметно ниже числа ядер;
снаружи слушают только 22, 80, 443 и порт VPN; всё остальное — на `127.0.0.1`; `fail2ban` показывает
два жжейла (`sshd` и защита шлюза от неавторизованных запросов).
**Плохо:** наружу торчит порт базы (5432), Redis или терминала — это находка, а не мелочь: закрывать
стражем/файрволом и сообщать владельцу.
**Грабли:** `docker ps` не показывает хостовые сервисы (Postgres, nginx) — «что у нас наружу» отвечается
только через `ss`, а не по списку контейнеров.

---

## 2. Проверка восстановления (restore drill)

Фраза-триггер: «проверь восстановление». Раздел отвечает на вопрос «мы правда восстановимся?», а не
«файлы на месте?». Копия, которая ни разу не разворачивалась, — это надежда.

### 2.1 Копии из ночного бэкапа

```bash
ls -lt /home/ai_service/ai-stack/backups/postgres/*.dump | head -4
```

Берём самый свежий дамп каждой базы и разворачиваем во временную базу (оригиналы не трогаем):

```bash
DUMP=$(ls -t /home/ai_service/ai-stack/backups/postgres/webui_db_*.dump | head -1)
TMP=verify_restore_manual_$(date +%Y%m%d_%H%M%S)
sudo -u postgres createdb "$TMP"
time sudo -u postgres pg_restore --no-owner --no-privileges -d "$TMP" "$DUMP"
sudo -u postgres psql -d "$TMP" -Atc "select count(*) from pg_tables where schemaname='public'"
sudo -u postgres psql -d "$TMP" -Atc "select count(*) from \"user\""
sudo -u postgres dropdb "$TMP"
```

**Норма:** `pg_restore` завершается без ошибок (предупреждения про роли и расширения допустимы),
таблиц столько же, сколько в живой базе, пользователи на месте. **Временную базу удалить после проверки**
— это её штатная судьба, но именно её и только её.
Повторить для `litellm_db`.

**Плохо:** `pg_restore: error` — дамп битый или снят с другой версии; таблицы есть, а данных нет —
дамп пустой (`pg_restore -l "$DUMP"` покажет оглавление).

### 2.2 Что уже проверяется само

- Ночью в 03:15 `monitor.py verify` разворачивает свежие дампы в `verify_restore_*` и удаляет их,
  отчёт уходит в бот уведомлений. Наличие осиротевших `verify_restore_*` — признак сбоя.
- Команда `/backups` в боте уведомлений делает то же самое по запросу.

Значит ручной прогон из 2.1 нужен не «чтобы проверить», а чтобы **измерить время** и убедиться, что
процедура работает целиком после изменений в установщике.

### 2.3 Файлы и состояние

```bash
python3 /usr/local/bin/webui-data-restore.py --help   # посмотреть режимы, ничего не восстанавливая
systemctl status webui-data-backup.timer --no-pager | tail -3
systemctl status secrets-offsite-backup.timer --no-pager | tail -3
```

**Норма:** таймеры активны, у файловых копий есть последние запуски. Архив секретов шифруется AES-256
и проверяется расшифровкой до отправки; ключ лежит на хосте, пароль — только у владельца.

### 2.4 Критерий приёмки прогона

Прогон считается успешным, если: обе базы развернулись без ошибок, число таблиц совпало, временные
базы удалены, в логах нет `error`, и замеренное время уложилось в ожидаемые 35–50 минут для полного
восстановления стека. В отчёт — фактическое время по шагам, а не оценка.

---

## 3. Проверка бэкапов и их доставки

```bash
tail -30 /home/ai_service/ai-stack/backups/backup.log
grep -c 'ok=True' /home/ai_service/notifications/registry.json 2>/dev/null || true
journalctl -u postgres-backup-notify.service -n 40 --no-pager
```

**Норма:** ночной дамп снят, отправлен, запись о `file_id` есть (без реестра файл в чате лежит, но
скачать его обратно нельзя). Копии режутся на части по 18 МБ: отправить можно больше, скачать — нет.
**Плохо:** «Сохранены бэкапы» в утреннем дайджесте — это **квитанция об отправке**, а не доказательство
восстановимости. Доказательство — раздел 2.

---

## 4. VPN-вход (отдельный контур, если развёрнут на этом хосте)

VPN ставится не установщиком стека, а отдельным репозиторием/скриптом (`/root/vpn-service`). Проверять
только если он действительно есть:

```bash
docker ps --format '{{.Names}}' | grep -x vpn-router
systemctl is-active nginx
ss -tlnp | grep -E ':(8443|2087)\b'
python3 /root/vpn-router/gemini_geo_check.py 2>/dev/null || true
```

**Норма:** контейнер `vpn-router` запущен, nginx слушает порт входа, подписка отдаётся
(`/sub/<token>` отвечает 200 и внутри ссылок правильный хост).
**Плохо:** туннель «подключается, но интернета нет» — сначала проверить DNS имени входа: мёртвая
A-запись выглядит как мёртвый VPN, при этом браузер работает из кэша. Затем — выход: страну
определяет IP соединения, а не домен.

---

## 5. Если что-то не так

Порядок для агента:

1. Собрать факты: `docker inspect`, `docker logs --tail 50`, `journalctl -u <unit> -n 50`, `nginx -t`, `ss -tlnp`.
2. Определить класс: сервис не стартовал / стартовал без конфига / работает, но не отвечает / отвечает ошибкой.
3. Исправить можно только безопасное: запустить/перезапустить сервис, поправить свой временный файл.
4. Всё остальное — остановиться и сформулировать владельцу: что именно нужно сделать и почему.
   Удаление, перезапись, `chmod/chown`, восстановление базы из дампа, правка `.env` — только с его «да».
5. В отчёте писать причину, а не симптом: «redis недоступен из-за неверного пароля» лучше, чем «шлюз не работает».

---

## 6. Формат отчёта

Не больше 25 строк, по-русски, без секретов и без простыней логов:

```
ВЕРДИКТ: всё по топологии | есть отклонения (N)

Проверено: контейнеры, HTTP, Postgres+pgvector, Redis, nginx/TLS, таймеры, бэкапы, боты, Hermes, ресурсы.
Отклонения:
- <что не так> → <что сделать или что нужно от владельца>
```

Если всё в порядке — одна строка вердикта и перечисление проверенного. Никаких «возможно» и «кажется»:
каждая строка подкреплена командой, которую можно повторить.
