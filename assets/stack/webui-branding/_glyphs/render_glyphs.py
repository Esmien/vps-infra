#!/usr/bin/env python3
"""Экспериментальная отрисовка упрощённых глифов для мелких размеров.

Не входит в сборку образа — это песочница для выбора направления.
Геометрия задана в нормированных координатах 0..100 (y вниз) и повторяет
силуэт logo_light.png (bbox 453x659, соотношение 0.687).

Рендер: суперсэмплинг x8 + LANCZOS вниз. Так мелкие размеры получаются
чище, чем при прямой отрисовке в целевой размер.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SS = 8  # суперсэмплинг
OUT = Path("out")
OUT.mkdir(exist_ok=True)

# ── геометрия силуэта ────────────────────────────────────────────────────────
T = (50.0, 0.0)      # верхняя вершина
UL = (16.0, 25.0)    # верхнее левое плечо
UR = (84.0, 25.0)    # верхнее правое плечо
L = (0.0, 58.0)      # левый «пояс»
R = (100.0, 58.0)    # правый «пояс»
B = (50.0, 100.0)    # нижний кончик
C = (47.0, 52.0)     # внутренний центр огранки

SILHOUETTE = [T, UR, R, B, L, UL]

# ── палитра ─────────────────────────────────────────────────────────────────
LAVENDER = "#A99CF0"
PALE_CYAN = "#CFE9F5"
VIOLET = "#7B6BE0"
TEAL = "#3FB6C8"
INDIGO = "#4B48A8"
DEEP_TEAL = "#237F92"

PRISM_L = "#5A52C8"
PRISM_R = "#46C2D2"

EDGE_DARK = "#2E3570"
EDGE_BRIGHT = "#7DD3E0"

TILE_BG = "#6C7BF5"
TILE_GLYPH = "#FFFFFF"


def variant_a() -> tuple[list[tuple[list[tuple[float, float]], str]], str | None]:
    """6 плоских фасетов — «богатый» вариант, ближе всего к исходнику."""
    return [
        ([T, UL, C], LAVENDER),
        ([T, C, UR], PALE_CYAN),
        ([UL, L, C], VIOLET),
        ([UR, C, R], TEAL),
        ([L, B, C], INDIGO),
        ([C, R, B], DEEP_TEAL),
    ], None


def variant_b() -> tuple[list[tuple[list[tuple[float, float]], str]], str | None]:
    """Призма: два тона, разрез по центральной оси. Самый живучий на 16 px."""
    return [
        ([(50, 0), UL, L, (50, 100)], PRISM_L),
        ([(50, 0), UR, R, (50, 100)], PRISM_R),
    ], None


def variant_c() -> tuple[list[tuple[list[tuple[float, float]], str]], str | None]:
    """Тёмный силуэт + яркая верхняя грань. Максимальный контраст формы."""
    return [
        (SILHOUETTE, EDGE_DARK),
        ([UL, UR, (50.0, 60.0)], EDGE_BRIGHT),
    ], None


def variant_d() -> tuple[list[tuple[list[tuple[float, float]], str]], str | None]:
    """Плитка: сплошной фирменный фон + одноцветный глиф. Для iOS/фавикона."""
    return [
        (SILHOUETTE, TILE_GLYPH),
        ([T, UL, C], "#D9DCFF"),
        ([UL, L, C], "#C3C9FF"),
    ], TILE_BG


VARIANTS = {
    "A фасеты": variant_a,
    "B призма": variant_b,
    "C грань": variant_c,
    "D плитка": variant_d,
}


def render(parts, tile_bg, size: int) -> Image.Image:
    """Рисует глиф в квадрат size×size через суперсэмплинг."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if tile_bg:
        # скругление ~22% — как у iOS-иконок
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=tile_bg)

    # глиф занимает 76% канвы: safe zone под круглый кроп ОС
    inset = big * 0.12
    span = big - 2 * inset
    sc = span / 100.0
    ox = oy = inset

    for pts, color in parts:
        d.polygon([(ox + x * sc, oy + y * sc) for x, y in pts], fill=color)

    return img.resize((size, size), Image.LANCZOS)


SIZES = [16, 20, 24, 32, 48, 64, 180]


def sheet(bg: str, name: str) -> None:
    """Контактный лист: строки — варианты, колонки — размеры."""
    label_w = 210
    pad = 18
    col_w = [max(s, 30) + pad * 2 for s in SIZES]
    row_h = max(SIZES) + pad * 2 + 16
    width = label_w + sum(col_w)
    height = row_h * len(VARIANTS) + 56

    canvas = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=17)
    small = ImageFont.load_default(size=14)

    # заголовки колонок
    x = label_w
    for s, w in zip(SIZES, col_w, strict=True):
        d.text((x + w // 2, 20), f"{s}px", fill="#8A8A8A", font=small, anchor="ma")
        x += w

    for row, (title, maker) in enumerate(VARIANTS.items()):
        parts, tile = maker()
        y0 = 56 + row * row_h
        d.text((16, y0 + row_h // 2), title, fill="#C8C8C8" if bg == "#1A1A1F" else "#333333",
               font=font, anchor="lm")
        x = label_w
        for s, w in zip(SIZES, col_w, strict=True):
            icon = render(parts, tile, s)
            canvas.paste(icon, (x + (w - s) // 2, y0 + (row_h - s) // 2 - 8), icon)
            x += w

    canvas.save(OUT / name)
    print("saved", OUT / name, canvas.size)


def zoom_strip(name: str) -> None:
    """24 px и 16 px, увеличенные в 10 раз по nearest — видно пиксельную сетку."""
    sizes = [16, 24]
    zoom = 10
    cell = max(sizes) * zoom + 40
    width = 210 + cell * len(sizes)
    height = (cell + 20) * len(VARIANTS) + 50
    canvas = Image.new("RGB", (width, height), "#101014")
    d = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=17)
    small = ImageFont.load_default(size=14)

    x = 210
    for s in sizes:
        d.text((x + cell // 2, 18), f"{s}px ×{zoom}", fill="#8A8A8A", font=small, anchor="ma")
        x += cell

    for row, (title, maker) in enumerate(VARIANTS.items()):
        parts, tile = maker()
        y0 = 50 + row * (cell + 20)
        d.text((16, y0 + cell // 2), title, fill="#C8C8C8", font=font, anchor="lm")
        x = 210
        for s in sizes:
            icon = render(parts, tile, s)
            big = icon.resize((s * zoom, s * zoom), Image.NEAREST)
            canvas.paste(big, (x + (cell - s * zoom) // 2, y0), big)
            x += cell

    canvas.save(OUT / name)
    print("saved", OUT / name, canvas.size)


def emit_svg() -> None:
    """SVG-исходники: пригодятся, когда появится нормальный вектор."""
    for title, maker in VARIANTS.items():
        parts, tile = maker()
        slug = title.split()[0].lower()
        body = []
        if tile:
            body.append(
                f'  <rect width="100" height="100" rx="22" fill="{tile}"/>'
            )
        for pts, color in parts:
            d = "M" + " L".join(f"{x:g} {y:g}" for x, y in pts) + " Z"
            body.append(f'  <path d="{d}" fill="{color}"/>')
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-6 -6 112 112" '
            'width="512" height="512">\n' + "\n".join(body) + "\n</svg>\n"
        )
        (OUT / f"glyph_{slug}.svg").write_text(svg)
        print("saved", OUT / f"glyph_{slug}.svg")


if __name__ == "__main__":
    emit_svg()
    sheet("#FFFFFF", "sheet_light.png")
    sheet("#1A1A1F", "sheet_dark.png")
    zoom_strip("zoom_24.png")
