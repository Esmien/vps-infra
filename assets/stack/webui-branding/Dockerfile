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
ARG OPENWEBUI_BASE=ghcr.io/open-webui/open-webui@sha256:4860ba005ae8f19f08d1e12b0868855b7b339ea0dad9927f196820fc0de04575

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

# 2) Ассеты в источник статики. Здесь и favicon/logo (цветной вариант),
#    и splash.png (светлая тема), и splash-dark.png (тёмная тема).
COPY --from=assets /branding/out/ /app/build/static/

LABEL org.opencontainers.image.title="Esmien AI (derived from Open WebUI)" \
      org.opencontainers.image.description="Open WebUI с кастомным брендингом Esmien AI" \
      org.opencontainers.image.source="https://github.com/open-webui/open-webui"
