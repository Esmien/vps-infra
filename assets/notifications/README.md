# Серверный мониторинг в Telegram

`monitor.py` использует Telegram-бота для сообщений (`BOT_TOKEN`, `BOT_OWNER`) и отдельного бота для файлов бэкапов (`FILE_BOT_TOKEN`). Для метрик нужен `psutil`, для контроля контейнеров — Docker CLI.

## Развёрнуто на текущем сервере

- Каталог скрипта: `/home/ai_service/notifications`.
- Бэкапы БД: `/home/ai_service/ai-stack/backups/postgres`, шаблон `*.dump`, retention — **3 последних дампа на каждую БД** (по количеству, не по дате).
- В `.env` сохранены Telegram-ключи и реальные пути/пороги мониторинга. Не публикуйте этот файл.
- Состояние и журнал: `/var/lib/server-monitor/` и `/var/log/server-monitor.log`.
- Cron-файл `/etc/cron.d/server-monitor` проверяет пороги/свежесть бэкапа каждые 10 минут. Опрос выполняется от root. Регулярной рассылки состояния сервера нет — метрики снимаются по запросу.
- События Docker (start / die / stop / restart / oom / create) отслеживает юнит `server-monitor-events.service` (`monitor.py events`, root, long-running, `Restart=on-failure`). Он подписан на Docker Events API, батчит события в окне `MONITOR_EVENT_WINDOW_SECONDS` (по умолчанию 60 с — это же и троттлинг, не чаще одного сообщения в окно), переподключается с экспоненциальным backoff (`tenacity`). Логи читаются только по факту события (`docker logs --tail N`), постоянной подписки на логи нет.
- Интерактивный бот: юнит `server-monitor-bot.service` (root, `monitor.py bot`, long polling `getUpdates`, `Restart=on-failure`) отвечает на команды владельца. Снапшот метрик создаётся в момент запроса; offset опроса хранится в `/var/lib/server-monitor/bot_state.json`, поэтому команды не теряются и не дублируются между перезапусками. Сообщения от чужих chat_id игнорируются.
- Бэкап: `postgres-backup.timer` — ежедневно **02:30 UTC (05:30 МСК)**, `backup-databases.sh` делает `pg_dump` обоих БД и оставляет только **3 последних дампа на каждую БД** (по количеству, `RETENTION_COUNT`, по умолчанию 3; имена БД выводятся из имён файлов, порядок — по mtime).
- Отчёт: `postgres-backup-notify.timer` — ежедневно **03:00 UTC (06:00 МСК)**, `monitor.py backup` отправляет свежие дампы в файловый бот и дайджест в бот сообщений.
- Боты: сообщения — `@esmien_vps_notificator_bot`, файлы — `@esmien_ai_backups_bot`. Владелец чата: `BOT_OWNER`. **Файловый бот может писать только после того, как владелец нажал Start в его чате.**

## Конфигурация

Текущие значения в `.env`:

```dotenv
BOT_TOKEN=<бот сообщений>
BOT_OWNER=214062001
FILE_BOT_TOKEN=<бот файлов>
BACKUP_DIR=/home/ai_service/ai-stack/backups/postgres
BACKUP_PATTERN=*.dump
MONITOR_DISK_PATH=/
MONITOR_STATE_FILE=/var/lib/server-monitor/state.json
MONITOR_ALERT_STATE_FILE=/var/lib/server-monitor/alerts.json
MONITOR_BOT_STATE_FILE=/var/lib/server-monitor/bot_state.json
MAX_BACKUP_AGE_HOURS=30
DISK_WARN_PERCENT=85
DISK_CRIT_PERCENT=95
INODE_WARN_PERCENT=85
INODE_CRIT_PERCENT=95
```

30 часов — дневной интервал бэкапа с запасом. Возраст вычисляется по `mtime` самого нового файла, совпавшего с шаблоном. Порог warning должен быть меньше critical.

## Команды

```bash
/usr/bin/python3 /home/ai_service/notifications/monitor.py events
/usr/bin/python3 /home/ai_service/notifications/monitor.py health
/usr/bin/python3 /home/ai_service/notifications/monitor.py bot
/usr/bin/python3 /home/ai_service/notifications/monitor.py alerts
/usr/bin/python3 /home/ai_service/notifications/monitor.py backup [--dry-run]
/usr/bin/python3 /home/ai_service/notifications/monitor.py verify
/usr/bin/python3 /home/ai_service/notifications/monitor.py send "текст"
```

- `events` — long-running наблюдатель за Docker Events API; в проде работает как `server-monitor-events.service`. Сообщения: старт/создание (последние 50 строк логов), штатная остановка (exit 0), падение (exit ≠ 0, последние 20 строк), `[OOM]` (OOMKilled + лимит/память хоста + маркеры ядра + последние 20 строк), циклический рестарт (один трейс на петлю, далее молчит до смены статуса; cooldown `MONITOR_RESTART_COOLDOWN_SECONDS`).
- `health` собирает и отправляет снапшот метрик в бот сообщений (то же, что делает команда `/health`).
- `bot` запускает интерактивный long-polling бот; в проде работает как `server-monitor-bot.service` (`systemctl restart server-monitor-bot`). Второй экземпляр не стартует — блокировка через `flock`.
- `alerts` отслеживает диск/inode и отсутствие/просрочку бэкапа, не спамит повтором.
- `backup` отправляет самый свежий дамп каждой БД в файловый бот (файлы > 45 МиБ режутся на 2 части `split -n 2`; инструкция по склейке уходит в файловый бот сразу под файлами) и дайджест в бот сообщений. `--dry-run` печатает план без обращения к Telegram.
- Целостность отправки: после каждой загрузки сверяется `file_size` документа из ответа Telegram с размером локального файла — расхождение даёт ошибку (файл ушёл не полностью). В инструкции по склейке указываются имена и размеры частей, готовая команда `cat` и ожидаемый SHA-256 собранного дампа, поэтому недокачанный или битый архив виден сразу.
- `verify` проверяет журнал бэкапа, свежесть дампов и статусы systemd-юнитов и отправляет отчёт в бот сообщений.
- `send` отправляет произвольный текст в бот сообщений.

## Самопроверка

Hermes-задача «Проверка ночного бэкапа PostgreSQL» (`job_id 3378eb7271b7`) запускается ежедневно в **03:15 UTC (06:15 МСК)** — после бэкапа и отчёта. Она вызывает `monitor.py verify` (отчёт уходит в бот сообщений) и при проблемах разбирается в причине и шлёт дополнительное сообщение.

## Интерактивные команды (бот сообщений)

- `/health` — снапшот метрик на момент запроса в чат владельца.
- `/healthcheck` — проверка работоспособности самого бота: отвечает фиксированным `200. Статус: ок`. Ответ идёт через Telegram Bot API, поэтому доставленный ответ доказывает, что long-polling цикл жив и бот может достучаться до Telegram. Используется внешними системами (Docker healthcheck, uptime-мониторы) как liveness-проба.
- `/docker_ps` — сводка по всем контейнерам: `<имя> — <Up|Exited|Restarting> | <uptime/downtime>`.
- `/backups` — дайджест последнего бэкапа БД (тот же текст, что отправляет ночной `monitor.py backup`) **плюс проверка валидности**: самый свежий дамп каждой БД реально разворачивается во временную БД (`verify_restore_<db>_<pid>_<ts>`) через `pg_restore --no-owner --no-privileges`, после чего временная БД удаляется. Дампы, которые Telegram получает разбитыми на части (`webui_db` > 45 МиБ), перед восстановлением заново режутся `split -n 2` и склеиваются — проверяется ровно тот байтовый поток, что приходит получателю. Занимает ~13 с (webui ~11 с, litellm ~1 с); на время проверки бот присылает «⏳ Проверяю…». Строится по текущему содержимому `BACKUP_DIR`.
- `/help` (и `/start`) — справка.
- Любой другой текст — подсказка со списком команд. Чужие chat_id игнорируются.

Формат сообщения `/health`:

```
🖥 ДАШБОРД СЕРВЕРА
────────────────────────────────

🧠 RAM
Занято: 4.0 GB | 51.1%, Свободно: 3.8 GB | 48.9%, Всего: 7.8 GB

⚙️ CPU
Количество ядер: 4 физических / 4 логических
Load Average: 0.54 / 0.31 / 0.27
Текущая утилизация: 1.2%

💿 Диск /
Занято: 28.8 GB | 30.1%, Свободно: 67.0 GB | 69.9%, Всего: 95.8 GB

Uptime: 5 days, 03:19:09
Rebooted: 06.10.2026, 21:13 (UTC+3)

💾 Топ-5 процессов по RAM:
1. python3 — 795.8 MB | 10.0%
...

⚡️ Топ-5 процессов по CPU:
1. python3 — 2.4%
...
```

Сбор метрик (выбор в пользу минимума ошибок и стабильности): RAM — `/proc/meminfo` (`MemTotal`/`MemAvailable`), CPU-тики — `/proc/stat`, load average — `os.getloadavg()`, uptime и время загрузки — `/proc/uptime`, диск — `shutil.disk_usage`, ядра — `psutil.cpu_count` с откатом на `/proc/cpuinfo`. Только топ процессов идёт через `psutil` (одно окно выборки ~0.8 с: прайминг счётчиков, сон, чтение), при отсутствии `psutil` сообщение всё равно собирается, кроме топов. RAM/CPU-тики/load/uptime падают на `psutil` только если `/proc` недоступен. Единицы RAM/диска: MB/GB (1 MB = 1024 KB, 1 GB = 1024 MB).

## Формат инструкции по склейке (файловый бот)

```
🧩 Архив отправлен двумя частями
Файл: webui_db_20261011_214123.dump (72.3 МиБ)

Части:
  1. webui_db_20261011_214123.dump.part_00 — 36.2 МиБ
  2. webui_db_20261011_214123.dump.part_01 — 36.2 МиБ

Как собрать (обе части в одной папке):
cat webui_db_20261011_214123.dump.part_00 webui_db_20261011_214123.dump.part_01 > webui_db_20261011_214123.dump

Проверка целостности (должна совпасть с указанным хешем):
sha256sum webui_db_20261011_214123.dump
Ожидаемый SHA-256: <hash>
```

Если хоть одна часть не загрузилась, вместо хеша печатается предупреждение «Часть файлов не доставлена» с командой повтора — ложной «успешной» инструкции не отправляется.

## Проверка бэкапа вручную

```bash
f=$(ls -t /home/ai_service/ai-stack/backups/postgres/webui_db_*.dump | head -1)
pg_restore -l "$f" | head            # читаемость архива и TOC (файл не обрезан)
stat -c '%s %y %n' "$f"              # размер и время создания
```

Полная проверка восстановлением — **только в отдельную временную БД, никогда поверх живой**:

```bash
sudo -u postgres createdb verify_restore_webui
sudo -u postgres pg_restore -d verify_restore_webui --no-owner --no-privileges "$f"
sudo -u postgres psql -tA -d verify_restore_webui -c "select count(*) from information_schema.tables where table_schema='public'"
sudo -u postgres dropdb verify_restore_webui
```

## Формат дайджеста

```
Создан бэкап webui_db (11.10.2026 06:30)
Создан бэкап litellm_db (11.10.2026 06:30)

Сохранены бэкапы:
webui_db:
  - Последний: 11.10.2026 06:30 (76.0 МиБ)
  - Средний: 10.10.2026 06:30 (73.7 МиБ)
  - Ранний: 09.10.2026 06:30 (73.7 МиБ)

litellm_db:
  - Последний: 11.10.2026 06:30 (1.0 МиБ)
  - Средний: 10.10.2026 06:30 (954.6 КиБ)
  - Ранний: 09.10.2026 06:30 (897.2 КиБ)

Общий размер: 626.9 МиБ

Проверка восстановлением (временная БД):
  ✅ webui_db: восстановлен из 2 частей (как в Telegram): 44 таблиц за 11.3 с
  ✅ litellm_db: восстановлен цельным файлом: 99 таблиц за 1.3 с
```

Секция «Проверка восстановлением» добавляется только командой `/backups` (по запросу); ночной `monitor.py backup` её не содержит.

## Питфоллы

- `/home/ai_service/ai-stack/ai/backup-databases.sh` должен быть **`755 root:root`**: юнит `postgres-backup.service` запускается от `User=ai_service`, и при более строгих правах (например `711`) запуск падает с `Permission denied`, `status=126` — бэкап не создаётся, а часы при этом выглядят «зелёными». Проверка: `systemctl start postgres-backup.service` и `Result=success`.
- Никогда не восстанавливайте дампы поверх живой БД: только `createdb verify_restore_*` → `pg_restore -d verify_restore_*` → `dropdb`.
- Части разбитого дампа (`*.part_00/01`) существуют только во время отправки и удаляются в `finally` — после прогона они не остаются на диске, повтор отправки берёт целый файл из бэкапа.
- Файловый бот может писать владельцу только если тот нажал Start в его чате; иначе `Bad Request: chat not found` и отправка файлов считается неуспешной.
- Тесты (`test_monitor.py`) обязаны молчать в сеть: `TELEGRAM_MOCKS` на уровне модуля подменяет `send_telegram`, `send_document` и `send_long_message`. Мокать только `send_long_message` **нельзя** — `notify_backup` шлёт дайджест через `send_telegram`, а вложения через `send_document`, и тестовые фикстуры по 1–2 КиБ реально улетали владельцу в бот (инцидент 12.10.2026). Проверка: прогон с `urlopen`, замоканным на исключение, должен проходить целиком.

## Откат

- Удаление `/etc/cron.d/server-monitor` остановит периодические проверки.
- `systemctl disable --now server-monitor-events.service` остановит мониторинг событий Docker.
- `systemctl disable --now server-monitor-bot.service` остановит интерактивного бота (команды `/health` и `/docker_ps` перестанут отвечать).
- `systemctl disable --now postgres-backup-notify.timer` отключит отчёт; `systemctl disable --now postgres-backup.timer` — сам бэкап.
- Исходный скрипт до подключения монитора: `/var/backups/backup-databases.sh.pre-monitor-20261005`.
- Бэкапы правок: `/home/ai_service/notifications/monitor.py.bak-<ts>`, `/etc/cron.d/server-monitor.bak-<ts>`.
