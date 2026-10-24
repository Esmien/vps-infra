# webui-branding — производный образ «Esmien AI»

Тонкая надстройка над официальным Open WebUI. Собирает локальный образ
`esmien/webui:local`, который:
1. убирает хардкод-суффикс ` (Open WebUI)` из названия;
2. подменяет иконки/лого/favicon на брендинг Esmien AI — **двух вариантов**:
   цветной для светлой темы и монохромный для тёмной.

## Файлы

| Файл | Назначение |
|---|---|
| `logo_light.png` | **исходник: цветной кристалл** (PNG с прозрачностью) → светлая тема, favicon, PWA |
| `logo_dark.png` | **исходник: монохромный хром** (PNG с прозрачностью) → `splash-dark.png` |
| `generate_assets.py` | генерит все ассеты из двух исходников |
| `Dockerfile` | 2 стадии: генерация ассетов → патч `env.py` + копирование ассетов |
| `cutout.py` | утилита: вырезает логотип с фона, если исходник пришёл JPEG-ом без альфы |
| `_state_snapshot/` | снимок docker-состояния «до» для отката (не трогать) |

## Какой ассет из какого исходника

| Ассет | Источник | Почему |
|---|---|---|
| `splash.png` | цветной | показывается на светлой теме |
| `splash-dark.png` | монохром | показывается на тёмной теме (`isDarkMode` в `index.html`) |
| `favicon.png`, `favicon-96x96.png`, `favicon.ico`, `favicon.svg` | цветной | favicon статичен, один на обе темы; цветной читается везде |
| `logo.png` | цветной | используется в PWA-манифесте, `background_color: #343541` |
| `apple-touch-icon.png`, `web-app-manifest-*.png` | цветной | иконки на домашнем экране |

> **Важно.** Только `splash` умеет переключаться по теме. Favicon и лого
> в манифесте — один файл на всё. Поэтому «чёрно-белый на тёмном» работает
> для splash, а иконка вкладки будет цветной всегда.

## Сборка и запуск

```bash
cd /home/ai_service/ai-stack/ai
docker compose build open-webui
docker compose up -d --force-recreate open-webui
```

Полная инструкция по откату и обновлению — в `../RESTORE_WEBUI.md`.

## Как поменять логотип

```bash
cd /home/ai_service/ai-stack/ai/webui-branding
cp /путь/к/цветному.png ./logo_light.png
cp /путь/к/моно.png    ./logo_dark.png
cd .. && docker compose build open-webui && docker compose up -d --force-recreate open-webui
```

После — жёсткий рефреш браузера (Ctrl/Cmd+Shift+R): favicon сидит в кэше.

**Требования к исходникам:** PNG с прозрачным фоном, квадратные,
объект по высоте не меньше 520px (самый крупный ассет — 512×512).
Если исходник пришёл JPEG-ом без альфы — сначала прогони через `cutout.py`:

```bash
python cutout.py in.jpg logo_light.png --bg light
```
