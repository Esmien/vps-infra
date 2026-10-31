# vps-infra

Идемпотентный установщик всей инфраструктуры VPS одной командой.
Разворачивает AI-стек (LiteLLM + Open WebUI + SearXNG + open-terminal),
PostgreSQL 17 с pgvector, Nginx с TLS, бэкапы и Telegram-мониторинг.
В финале предлагает поставить Hermes в хостовом режиме.

Бот с музыкой (lyria) намеренно **не** входит в этот репозиторий.

## Быстрый старт

На чистом Ubuntu 22.04/24.04 (root):

```bash
curl -fsSL https://raw.githubusercontent.com/Esmien/vps-infra/main/bootstrap.sh | sudo bash
```

Установщик спросит домены, e-mail для Let's Encrypt и внешние секреты
(OpenRouter, Telegram, GitHub), остальные секреты сгенерирует сам и запишет
в `ai/.env` с правами `0640 root:ai_service`.

Без вопросов (значения из существующих файлов или дефолты):

```bash
curl -fsSL …/bootstrap.sh | sudo bash -s -- --yes
```

## Переезд на новый сервер

**Одной командой (рекомендуется).** Дампы и состояние Hermes тянутся сами:

```bash
curl -fsSL …/bootstrap.sh | sudo bash -s -- --from-bot --restore --hermes --restore-hermes
```

- `--from-bot` — установщик спрашивает токен файл-бота, берёт из реестра `file_id`
  последний набор дампов, качает части, склеивает и сверяет `sha256`;
- `--restore-hermes` — из приватного `Esmien/hermes-state` разворачиваются
  `config.yaml`, `SOUL.md`, memories, skills, cron, `/usr/local/bin`, юниты, `/etc`;
- секреты (`ai/.env`, `notifications/.env`, `/root/.hermes/.env`) в репозиториях
  не лежат — установщик спросит их заново (GITHUB_KEY, OpenRouter, токены ботов).

Без флагов те же вопросы задаются интерактивно: «тянуть бэкап из бота?» и
«развернуть состояние Hermes?».

**Вручную (bundle).**

1. На старом сервере собрать переносимый bundle:

   ```bash
   sudo /opt/vps-infra/install.py export --with-data
   # → /var/lib/vps-infra/vps-infra-bundle-YYYYmmdd-HHMMSS.tar.gz
   ```

   Внутри: свежие дампы `webui_db` и `litellm_db`, данные Open WebUI
   (`webui_data`) и манифест. Секретов внутри нет — их установщик спросит заново.

2. Перенести файл на новый сервер (scp / телеграм-бот для файлов).

3. На новом сервере:

   ```bash
   curl -fsSL …/bootstrap.sh | sudo bash -s -- \
       --bundle /root/vps-infra-bundle.tar.gz --restore
   ```

   `--restore` нужен, чтобы восстановление сработало поверх уже созданных
   пустых баз. Без `--restore` установщик восстанавливает БД только если они пусты.

4. Прописать DNS-записи доменов на IP нового сервера **до** шага TLS,
   иначе сертификаты не выпустятся (установщик предупредит и оставит HTTP).

### Как бэкап попадает в бота и возвращается назад

- `monitor.py backup` режет каждый дамп на части **≤18 МБ** (лимит `getFile` —
  20 МБ; с частями по 45 МБ файл из чата скачать уже нельзя) и шлёт их в
  файл-бот вместе с инструкцией склейки.
- На каждую часть сохраняется `file_id` в
  `backups/postgres/.tg-registry.json` (последние 3 набора на базу). Бот не
  читает свою историю чата, поэтому `file_id` — единственная зацепка.
- Реестр уезжает в приватный `hermes-state` (`host/backups/tg-registry.json`),
  откуда его берёт `install.py --from-bot` (нужен `GITHUB_KEY`).
- Если реестра нет (совсем новый контур, GitHub недоступен) — fallback:
  переслать файлы бэкапа самому боту, установщик найдёт их через `getUpdates`.

## Команды

```bash
install.py                     # интерактивная установка
install.py --yes               # без вопросов
install.py check               # только проверка, ничего не меняет
install.py export [--with-data]# bundle для переезда
install.py --only stack,units,verify
install.py --skip tls,hermes
install.py --bundle <path|url> --restore
install.py --from-bot          # дампы из Telegram-бота (спросит токен)
install.py --tg-registry <path|url> --tg-token <token>
install.py --restore-hermes    # состояние Hermes из hermes-state
install.py --dry-run           # показать план
```

Шаги: `preflight, packages, users, stack, secrets, bundle, fetch, postgres,
network, images, migrate, restore, apps, notifications, beszel, units, hardening, tls,
verify, hermes`.

`hardening` ставит fail2ban (jail.local: sshd, бан 1д+, повторным — до 4 недель,
`ignoreip` включает docker-сети) и кладёт nginx-зоны rate-limit
(`/etc/nginx/conf.d/zz-ratelimit.conf`). Лимиты: `llm.` — весь API 10 r/s,
`ai.` — только `/api/` 8 r/s (статика WebUI намеренно без лимита: иначе F5
со сотней чанков получает 429 и страница не грузится). Сверх лимита — 429.

## Что идемпотентно

Каждый шаг сначала смотрит на фактическое состояние:

* пакеты ставятся только отсутствующие;
* `ai/.env` перезаписывается только при изменении; существующие секреты
  переиспользуются (переспросить — интерактивный запуск, Enter = оставить);
* роли/базы создаются при отсутствии, пароли ролей приводятся к `.env`;
* образы с `:latest` не обновляются, если уже есть локально (осознанно:
  повторный запуск не должен молча менять версии); WebUI-образ собирается,
  если его нет;
* БД восстанавливаются только в пустые базы или по явному `--restore`;
* vhost'ы и юниты пишутся только при изменении, таймеры включаются повторно
  безвредно.

## Авто-коммит и пуш (как у hermes-state)

Рабочая копия `/root/vps-infra` — источник правды. Изменения уезжают в GitHub
сами, по факту правки, без таймера:

- `vps-infra-watch.service` — inotify на каталог, debounce 90 с (max 15 мин),
  старт сервиса = догоняющий пуш;
- `vps-infra-push.sh` — `maintainer/sync_assets.py` подтягивает живые файлы
  хоста в `assets/` (без `.env`, `__pycache__`, тестов и снапшотов), затем
  проверяет, что `install.py` компилируется, `bootstrap.sh` проходит `bash -n`,
  compose валидируется на фиктивных секретах;
- секрет-скан: при находке (`ghp_`, `sk-`, PEM, bot-token) коммит отменяется;
- коммит создаётся только при реальных изменениях; push догоняет origin,
  при сбое — ретрай с бэкоффом и алерт в Telegram не чаще раза в 30 минут.

```bash
systemctl status vps-infra-watch
systemctl start vps-infra-push      # принудительный прогон
tail -n 20 /var/log/vps-infra-autopush.log
```

## Раскладка

```
install.py                  оркестратор
bootstrap.sh                curl | bash
assets/stack/               compose-стек, конфиги LiteLLM/SearXNG, брендинг WebUI
assets/notifications/       monitor.py (алерты, /health, бэкапы)
assets/host/bin/            скрипты в /usr/local/bin
assets/host/systemd/        юниты и таймеры
assets/host/open-terminal-config/  .bashrc и chroot-host для open-terminal
assets/templates/           шаблоны .env
maintainer/                 авто-коммит/пуш репозитория (systemd + скрипты)
```

Стек разворачивается в `/home/ai_service/ai-stack` (меняется `--infra-dir`),
пользователь `ai_service` — владелец файлов и бэкапов.

## Секреты

В репозитории нет ни одного реального секрета. Всё, что нужно, лежит в:

* `/home/ai_service/ai-stack/ai/.env` — ключи стека (0640 root:ai_service)
* `/home/ai_service/notifications/.env` — Telegram-токены (0640 root:root)
* `/etc/vps-infra/install.json` — несекретные параметры (0640 root)

`.gitignore` исключает `.env`, bundle-архивы, дампы и локальные логи.

## После установки

* Первый администратор Open WebUI создаётся через форму регистрации на
  домене WebUI (на восстановленной БД пользователи уже есть).
* Админка LiteLLM — `https://<llm-домен>` с `UI_USERNAME`/`UI_PASSWORD`.
* Бэкапы БД — ежедневно, `systemctl list-timers postgres-backup.timer`.
* Мониторинг — `systemctl status server-monitor-bot server-monitor-events`.

### Beszel — графики и пороги (шаг `beszel`)

Лёгкий мониторинг хоста: hub + агент в Docker, на этой машине вместе ~20 МБ RAM.
Hub слушает только `127.0.0.1:8090`, наружу не публикуется.

* Файлы: `/home/ai_service/beszel/{docker-compose.yml,.env}`, данные в `beszel_data/`.
* Связка hub↔агент — **unix-сокет** `beszel_socket/beszel.sock` (SSH-ключи не нужны).
  `KEY` в `.env` — публичный ключ хаба (`beszel_data/id_ed25519`), `TOKEN` — токен
  системы из коллекции `fingerprints` (в UI создаётся сам, при ручной настройке:
  `ssh-keygen -y -f beszel_data/id_ed25519`).
* Алерты: `Status` (упал), `CPU>85`/10 мин, `Memory>90`/10 мин, `Disk>85`/30 мин,
  доставка — webhook `telegram://<BOT_TOKEN>@telegram?chats=<chat_id>&parseMode=HTML`
  в `user_settings.settings.webhooks` (токен хранится только в локальной БД).
* Доступ снаружи: `mon.<base-domain>` → A-запись на сервер. На чистой установке
  домен попадает в `step_tls` автоматически (`sites[mon] = 8090`), сертификат
  выпускает certbot. На живом хосте без DNS — SSH-туннель:
  `ssh -L 8090:127.0.0.1:8090 root@<ip>` → `http://localhost:8090`.
* **Let's Encrypt лимит: 50 сертификатов на зарегистрированный домен за 168 ч.**
  Если certbot ответил `too many certificates ... retry after <время>` — ставить
  `mon-cert-setup.timer` (в `assets/host/{bin,systemd}`), он ретраит каждые 30 мин,
  при успехе сам кладёт HTTPS-vhost из `assets/host/nginx/mon.<domain>.ssl`,
  переключает `APP_URL` на https и присылает уведомление.)
* Логин админа — `/etc/beszel-admin.env` (0600 root).

### Внешний мониторинг: GitHub Actions (работает из коробки)

`.github/workflows/uptime.yml` cron `*/10 * * * *` дергает публичные домены
**с раннеров GitHub** — то есть проверяет сервер снаружи, независимо от него.
Нет 200 с трёх попыток подряд → сообщение в Telegram (secret `TG_BOT_TOKEN`,
chat 214062001) + письмо GitHub о падении воркфлоу.

* Секрет уже заведён: `gh secret set TG_BOT_TOKEN` или через API (encrypted_value).
* Drill без ожидания реального падения:
  `gh workflow run uptime.yml -f extra_url=https://httpbin.org/status/500` —
  прогон упадёт и пришлёт сообщение с префиксом `[ТЕСТ]`.
* **Публичные репо: GitHub отключает `schedule` после 60 дней без активности.**
  Поэтому `uptime-heartbeat.timer` раз в месяц дописывает строку в
  `maintainer/uptime-heartbeat.txt` — watcher коммитит, расписание живёт.
* Точность: GitHub cron ходит с разбросом ±5–15 мин, обнаружение — до ~20 мин.
  Нужно быстрее/точнее — второй контур на healthchecks.io (см. ниже).

### Внешний dead-man switch (healthchecks.io, опционально)

Внутренний мониторинг живёт на этой же машине и умирает вместе с ней. Чтобы
узнавать о полном падении (хост, сеть, провайдер), есть `deadman-ping.timer`:
раз в 5 минут пингует внешний чек-сервис (healthchecks.io и совместимые),
пока стек жив; если контейнер не `healthy` / nginx / fail2ban лежит — пинг идёт
на `<url>/fail`, и алерт приходит сразу, а не через grace.

Подключение (вручную, один раз — сервис внешний, нужен аккаунт):

1. healthchecks.io → создать чек: `Period = 15 min`, `Grace = 5 min`,
   интеграция уведомлений — Telegram (или e-mail).
2. Положить ping-URL в `/etc/deadman-ping.url` (0640 root) или строкой
   `DEADMAN_URL=<url>` в `/home/ai_service/notifications/.env`.
3. Проверка: `systemctl start deadman-ping.service; journalctl -u deadman-ping -n 5`
   и «Last ping» в кабинете healthchecks.

Без URL скрипт молча выходит (код 0) — таймер можно держать включённым всегда.
