# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Esmien AI — производный (derived) образ Open WebUI.
#
# Что делает:
#   1) убирает хардкод-суффикс " (Open WebUI)" у WEBUI_NAME (rebrand);
#   2) кладёт кастомные ассеты брендинга в ИСТОЧНИК статики
#      (/app/build/static). При каждом старте контейнера копировальщик
#      backend/open_webui/config.py заново наполняет из него раздаваемую
#      папку /app/backend/open_webui/static — правки «на живом» контейнере
#      всё равно потерялись бы, поэтому ассеты живут в образе.
#
# Лицензия: Open WebUI v0.6.6+ разрешает ребрендинг для self-hosted
# инсталляций с <=50 пользователями (aggregate). Подробнее:
# https://docs.openwebui.com/license
#
# Обновление базового образа (единственная точка правки дайджеста — ниже):
#   docker pull ghcr.io/open-webui/open-webui:main
#   docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/open-webui/open-webui:main
#   подставить новый sha256:... в OPENWEBUI_BASE, затем пересобрать.
# ---------------------------------------------------------------------------

# Глобальный ARG (объявлен ДО первого FROM, иначе в FROM не виден).
# Пиннинг по дайджесту: :main — rolling-тег, один и тот же тег со временем
# указывает на разный код. Дайджест фиксирует конкретную сборку.
# Текущая база: Open WebUI v0.13.3 (2026-11-08).
ARG OPENWEBUI_BASE=ghcr.io/open-webui/open-webui@sha256:46b59158e6558eeb980bc828e0f1b30238ad24d82ed84d5e5eda1dab2a082f14

# ── stage 1: генерация всех ассетов брендинга из двух исходников ─────────────
FROM python:3.12-slim AS assets
RUN pip install --no-cache-dir pillow
WORKDIR /branding
COPY logo_light.png logo_dark.png generate_assets.py ./
RUN python generate_assets.py

# ── stage 2: производный образ Open WebUI ────────────────────────────────────
FROM ${OPENWEBUI_BASE}

# 1) Убираем суффикс. grep-проверка намеренно ВАЛИТ сборку, если апстрим
#    переименует/подвинет строку — лучше явная ошибка, чем тихо собранный
#    образ с вернувшимся "(Open WebUI)".
RUN sed -i \
        "s/    WEBUI_NAME += ' (Open WebUI)'/    pass  # rebranded: Esmien AI/" \
        /app/backend/open_webui/env.py \
 && grep -q "rebranded: Esmien AI" /app/backend/open_webui/env.py \
 && rm -f /app/backend/open_webui/__pycache__/env.cpython-*.pyc

# 1b) Cache-busting иконок: iOS кэширует apple-touch-icon по URL намертво,
#     чистка данных Safari его не сбрасывает. Новые имена файлов = новые URL.
RUN sed -i \
        -e "s|/static/apple-touch-icon.png|/static/apple-touch-icon-v2.png|g" \
        -e "s|rel=\"icon\" type=\"image/png\" href=\"/static/favicon.png\"|rel=\"icon\" type=\"image/png\" href=\"/static/favicon-v2.png\"|g" \
        /app/build/index.html \
 && sed -i "s|/static/logo.png|/static/logo-v2.png|g" /app/backend/open_webui/main.py \
 && grep -q "apple-touch-icon-v2.png" /app/build/index.html \
 && grep -q "logo-v2.png" /app/backend/open_webui/main.py \
 && rm -f /app/backend/open_webui/__pycache__/main.cpython-*.pyc

# 1c) Лендинг v0.13.0+: бейдж над заголовком захардкожен в бандле ("Open WebUI"),
#     WEBUI_NAME на него не влияет — правим строку в собранном чанке.
RUN for f in /app/build/_app/immutable/nodes/*.js; do \
        sed -i "s|>Open WebUI</div>|>Esmien AI</div>|g" "$f"; \
    done \
 && ! grep -rq ">Open WebUI</div>" /app/build/_app/immutable/nodes/ \
 && echo "landing badge rebranded"

# 2) Ассеты в источник статики. Здесь и favicon/logo (цветной вариант),
#    и splash.png (светлая тема), и splash-dark.png (тёмная тема).
COPY --from=assets /branding/out/ /app/build/static/

# 3) Turnstile-гейт регистрации: JS-файл + тег в index.html. Скрипт сам решает,
#    показывать виджет или нет (если ключи не заданы — молчит).
COPY turnstile-gate.js /app/build/static/turnstile-gate.js
RUN sed -i 's|<link rel="stylesheet" href="/static/custom.css" crossorigin="use-credentials" />|&\n\t\t<script src="/static/turnstile-gate.js?v=2" defer crossorigin="use-credentials"></script>|' /app/build/index.html \
 && grep -q "turnstile-gate.js" /app/build/index.html

LABEL org.opencontainers.image.title="Esmien AI (derived from Open WebUI)" \
      org.opencontainers.image.description="Open WebUI с кастомным брендингом Esmien AI" \
      org.opencontainers.image.source="https://github.com/open-webui/open-webui"
