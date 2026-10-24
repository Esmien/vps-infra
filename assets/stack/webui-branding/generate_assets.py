#!/usr/bin/env python3
"""Генерация всех брендинг-ассетов Open WebUI из двух исходников.

Вход (оба — PNG с прозрачным фоном, квадратные, желательно >= 768px):
    logo_light.png — цветной кристалл  -> светлая тема, favicon, PWA
    logo_dark.png  — монохромный хром  -> тёмная тема (splash-dark)

Выход: out/<ассеты, которые Open WebUI ищет в /static>

Почему именно так (проверено по коду v0.12.3):
    * index.html переключает splash по теме: isDarkMode ? splash-dark.png
      : splash.png — единственный ассет с theme-awareness;
    * favicon.png / favicon-96x96.png / favicon.ico / apple-touch-icon.png
      и logo.png статичны — один файл на обе темы. Поэтому для них берётся
      цветной вариант: на тёмном фоне цветной кристалл читается,
      а монохром на белом выглядит бледно.

Запускается на этапе сборки образа (stage "assets" в Dockerfile).
Локально:  pip install pillow && python generate_assets.py
"""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image

OUT = Path("out")
LIGHT_SRC = Path("logo_light.png")
DARK_SRC = Path("logo_dark.png")

# Размеры повторяют стоковые ассеты Open WebUI (сверено с v0.12.3).
# padding — доля поля вокруг логотипа: для maskable-иконок больше,
# чтобы круглый кроп ОС не срезал вершины кристалла.
ASSETS: dict[str, tuple[str, int, float]] = {
    "favicon.png": ("light", 512, 0.06),
    "favicon-96x96.png": ("light", 96, 0.04),
    "favicon.ico": ("light", 32, 0.04),
    "apple-touch-icon.png": ("light", 180, 0.10),
    "logo.png": ("light", 500, 0.06),
    "web-app-manifest-192x192.png": ("light", 192, 0.12),
    "web-app-manifest-512x512.png": ("light", 512, 0.12),
    "splash.png": ("light", 500, 0.08),
    "splash-dark.png": ("dark", 500, 0.08),
}


def fit_square(img: Image.Image, size: int, padding: float) -> Image.Image:
    """Обрезает по содержимому, центрирует на прозрачном квадрате, ресайзит.

    Логотип у нас вытянут по вертикали (портрет). Если ресайзить его
    «в лоб» до size×size, кристалл сплющится. Поэтому сначала вписываем
    в квадрат с сохранением пропорций, потом масштабируем.
    """
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox is None:
        raise SystemExit("Пустой альфа-канал: в исходнике нет видимого логотипа")
    img = img.crop(bbox)

    side = int(max(img.size) * (1 + 2 * padding))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    for p in (LIGHT_SRC, DARK_SRC):
        if not p.exists():
            raise SystemExit(f"Не найден исходник {p.resolve()}")

    sources = {"light": Image.open(LIGHT_SRC), "dark": Image.open(DARK_SRC)}
    OUT.mkdir(parents=True, exist_ok=True)

    for name, (variant, size, padding) in ASSETS.items():
        out = fit_square(sources[variant], size, padding)
        if name.endswith(".ico"):
            out.save(OUT / name, sizes=[(16, 16), (32, 32), (48, 48)])
        else:
            out.save(OUT / name, optimize=True)

    # favicon.svg у Open WebUI — это SVG с ВШИТОЙ base64-PNG (не вектор).
    # Повторяем тот же формат, чтобы поведение совпадало с апстримом.
    png_b64 = base64.b64encode((OUT / "favicon.png").read_bytes()).decode()
    (OUT / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="512" height="512" viewBox="0 0 512 512">'
        f'<image width="512" height="512" xlink:href="data:image/png;base64,{png_b64}"/>'
        "</svg>"
    )

    print("Сгенерировано:", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
