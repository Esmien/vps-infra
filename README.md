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

## Команды

```bash
install.py                     # интерактивная установка
install.py --yes               # без вопросов
install.py check               # только проверка, ничего не меняет
install.py export [--with-data]# bundle для переезда
install.py --only stack,units,verify
install.py --skip tls,hermes
install.py --bundle <path|url> --restore
install.py --dry-run           # показать план
```

Шаги: `preflight, packages, users, stack, secrets, bundle, postgres, network,
images, migrate, restore, apps, notifications, units, tls, verify, hermes`.

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
