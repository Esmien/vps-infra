# RESTORE_WEBUI.md — откат брендинга Open WebUI (Esmien AI)

Документ описывает, как **полностью вернуть стоковый Open WebUI** и как
пересобрать наш производный образ. Держи его рядом с `docker-compose.yml`.

Рабочая директория стека:

```
/home/ai_service/ai-stack/ai
```

---

## 0. TL;DR — быстрый откат к стоковому Open WebUI (одна команда)

```bash
cd /home/ai_service/ai-stack/ai
cp docker-compose.yml.bak.20261022_200945 docker-compose.yml
docker compose up -d --force-recreate open-webui
```

Это вернёт сервис `open-webui` на официальный образ
`ghcr.io/open-webui/open-webui:main` и уберёт `WEBUI_NAME`.
Кастомный образ `esmien/webui:local` при этом останется лежать локально —
при желании удали его вручную (см. раздел 4).

---

## 1. Что именно мы изменили (и где бэкапы)

| Что | Где лежит | Бэкап для отката |
|---|---|---|
| `docker-compose.yml` | `/home/ai_service/ai-stack/ai/docker-compose.yml` | `docker-compose.yml.bak.20261022_200945` |
| Проект брендинга | `/home/ai_service/ai-stack/ai/webui-branding/` | — (сам является надстройкой, удаляется целиком) |
| Образ `esmien/webui:local` | локальный docker-образ | пересобирается из `webui-branding/` |

Точный diff по compose сохранён в:
`webui-branding/_state_snapshot/compose.diff.patch`

Снапшот docker-состояния «до» (для разбора инцидентов):
`webui-branding/_state_snapshot/`
- `open-webui.container.inspect.json` — состояние контейнера до изменений;
- `open-webui.image.inspect.json` — стоковый образ до изменений;
- `compose.resolved.before.yml` — развёрнутый `docker compose config` до изменений;
- `branding.sha256` — контрольные суммы файлов брендинга.

Контрольные значения на момент внедрения:
- стоковый базовый образ: `ghcr.io/open-webui/open-webui@sha256:a5b9e403d19154796850679c8c5d1295301cee954be8b84ddd62af92426558f7` (версия 0.12.4, образ от 2026-10-25). Предыдущая база — 0.12.3, `@sha256:4860ba005ae8f19f08d1e12b0868855b7b339ea0dad9927f196820fc0de04575` — годится как пин для откатов;
- собранный образ: `esmien/webui:local` (пересобирается из `webui-branding/`).

### Схема брендинга: два исходника на две темы

| Ассет | Источник | Когда показывается |
|---|---|---|
| `splash.png` | `logo_light.png` (цветной) | светлая тема |
| `splash-dark.png` | `logo_dark.png` (монохром) | тёмная тема |
| `favicon*`, `logo.png`, PWA-иконки | `logo_light.png` (цветной) | всегда — один файл на обе темы |

Только splash переключается по теме (`isDarkMode` в `index.html`). Favicon
статичен — разделить его на светлый/тёмный вариант в Open WebUI нельзя.

---

## 2. Полный откат (пошагово)

### Вариант A. Только вернуть стоковый образ (брендинг был лишним)

```bash
cd /home/ai_service/ai-stack/ai

# 1) восстановить compose из бэкапа
cp docker-compose.yml.bak.20261022_200945 docker-compose.yml

# 2) проверить, что конфиг валиден
docker compose config >/dev/null && echo OK

# 3) пересоздать только open-webui
docker compose up -d --force-recreate open-webui

# 4) проверить
curl -sf http://127.0.0.1:8080/health && echo HEALTHY
curl -s http://127.0.0.1:8080/api/config | head -c 200   # ожидаем "name":"Open WebUI"
```

### Вариант B. Полное удаление следов брендинга

```bash
cd /home/ai_service/ai-stack/ai

# compose назад
cp docker-compose.yml.bak.20261022_200945 docker-compose.yml
docker compose up -d --force-recreate open-webui

# удалить проект брендинга (снапшот тоже; если он ещё нужен — скопируй себе)
rm -rf webui-branding

# удалить локальный образ
docker rmi esmien/webui:local
```

> **Данные НЕ пострадают.** Все чаты/аккаунты/настройки лежат в
> `/home/ai_service/webui_data` (volume `/app/backend/data`) и никак
> не трогаются — мы меняем только образ и статику внутри него.

---

## 3. Как пересобрать / обновить брендинг

### Сменить логотип

```bash
cd /home/ai_service/ai-stack/ai/webui-branding
# исходники — PNG с прозрачным фоном (лучше >= 768px):
cp /путь/к/цветному.png ./logo_light.png
cp /путь/к/монохромному.png ./logo_dark.png
cd .. && docker compose build open-webui && docker compose up -d --force-recreate open-webui
```

Если исходник пришёл JPEG-ом (без альфы) — сначала убери фон:

```bash
cd /home/ai_service/ai-stack/ai/webui-branding
python cutout.py in.jpg logo_light.png --bg light
```

Все ассеты (favicon, logo, splash для двух тем, apple-touch, PWA-иконки,
favicon.svg) пересобираются автоматически на этапе сборки образа —
см. `webui-branding/generate_assets.py`.
После пересборки **жёстко обнови браузер** (Ctrl/Cmd+Shift+R): favicon
агрессивно кешируется.

Проверить, что сервис отдаёт именно наши иконки (без браузерного кеша):

```bash
curl -s http://127.0.0.1:8080/static/splash-dark.png | sha256sum
sha256sum webui-branding/out/splash-dark.png   # суммы должны совпасть
```

### Как обновить базовый образ Open WebUI до свежего

1. Стянуть свежий апстрим и узнать его дайджест:
   ```bash
   docker pull ghcr.io/open-webui/open-webui:main
   docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/open-webui/open-webui:main
   ```
2. Подставить новый `sha256:...` в `ARG OPENWEBUI_BASE` в
   `webui-branding/Dockerfile` (это **единственное** место с дайджестом).
3. Пересобрать и пересоздать:
   ```bash
   cd /home/ai_service/ai-stack/ai
   docker compose build --pull open-webui
   docker compose up -d --force-recreate open-webui
   ```

> **Почему дайджест, а не `:main`.** `:main` — rolling-тег: один и тот же
> тег со временем указывает на разный код (у нас уже переезжал 6 дней назад).
> Дайджест фиксирует конкретную сборку, чтобы «вчера было ок, сегодня
> суффикс вернулся» не случалось.

> **Если апстрим переименует строку в `env.py`**, сборка УПАДЁТ на проверке
> `grep -q "rebranded: Esmien AI"` — это намеренно. Тогда нужно открыть
> `webui-branding/Dockerfile` и поправить `sed`-шаблон под новую строку
> суффикса (искать `WEBUI_NAME += ' (Open WebUI)'` в свежем `env.py`).

---

## 4. Диагностика

| Симптом | Причина / что делать |
|---|---|
| В заголовке вкладки снова `(Open WebUI)` | Пересборка не выполнялась или базовый образ подтянулся без патча. `docker compose build --pull open-webui && docker compose up -d --force-recreate open-webui`. |
| Favicon не поменялся | Кеш браузера. Ctrl/Cmd+Shift+R, или проверь `curl -s http://127.0.0.1:8080/static/favicon.png \| sha256sum` против своего файла. |
| Сборка падает на `grep -q "rebranded"` | Апстрим переименовал строку суффикса. Поправь `sed` в Dockerfile (см. раздел 3). |
| Сборка падает на `base name ... should not be blank` | `ARG OPENWEBUI_BASE` объявлен не в самом верху Dockerfile (до первого `FROM`). |
| Сервис не поднялся после пересоздания | Проверь, что базовый дайджест существует: `docker pull <digest-строка>`. При откате просто верни `docker-compose.yml.bak.*`. |

Проверить, что суффикс реально убран внутри собранного образа:

```bash
docker run --rm --entrypoint sh esmien/webui:local -c \
  "grep -n 'Open WebUI)' /app/backend/open_webui/env.py || echo 'NO SUFFIX (good)'"
```

---

## 5. Лицензия (важно)

Open WebUI v0.6.6+ содержит branding protection clause: изменять/скрывать
брендинг нельзя, **кроме** случаев, один из которых — self-hosted
инсталляция с **≤50 пользователями (aggregate)**. Наш инстанс попадает
под это исключение (`ENABLE_SIGNUP=False`, один пользователь).
Если инстанс вырастет до 50+ пользователей или станет коммерческим —
нужна enterprise-лицензия либо форк от v0.6.5 (чистый BSD-3).
Подробнее: https://docs.openwebui.com/license
