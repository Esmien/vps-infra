#!/usr/bin/env python3
"""Вырезает логотип с фона и делает RGBA-PNG с прозрачностью.

Зачем: исходники приходят как JPEG со светлым (или тёмным) фоном, а ассеты
Open WebUI должны работать и на светлой, и на тёмной теме. Поэтому фон
убирается в альфу, а не остаётся «плитой» цвета исходника.

Алгоритм (рассчитан на объект, заметно отличающийся по яркости от фона):
  1. маска фона = пиксели, близкие к цвету углов, СВЯЗАННЫЕ с рамкой кадра
     (так светлые блики ВНУТРИ логотипа не считаются фоном);
  2. объект = дополнение; берём крупнейшую компоненту, заливаем дырки;
  3. мягкий край: лёгкое размытие альфы, чтобы не было «лесенки»;
  4. обрезка по содержимому с полями (padding), приведение к квадрату —
     иначе иконки 1:1 сплющат логотип.

Память: держим в работе ~4 float32-копии кадра. Для 2048² это ~250-350 МБ,
для 4096² — до 1.2 ГБ. Не гоняй параллельно с тяжёлыми контейнерами.

Использование:
    python cutout.py in.jpg out.png [--bg auto|light|dark] [--padding 0.06]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def build_background_mask(arr: np.ndarray, bg: str) -> np.ndarray:
    """Маска фона: цветовые кластеры у рамки кадра, а не просто порог яркости."""
    h, w = arr.shape[:2]
    band = max(8, min(h, w) // 64)
    corners = np.concatenate(
        [
            arr[:band, :band].reshape(-1, 3),
            arr[:band, -band:].reshape(-1, 3),
            arr[-band:, :band].reshape(-1, 3),
            arr[-band:, -band:].reshape(-1, 3),
        ]
    ).astype(np.float32)
    ref = np.median(corners, axis=0)

    dist = np.linalg.norm(arr.astype(np.float32) - ref, axis=2)
    # допуск: фон почти однородный, берём запас на JPEG-шум и лёгкий градиент
    tol = 28.0 if bg in ("auto", "light") else 34.0
    candidate = (dist < tol).astype(np.uint8)

    # оставляем только то, что связано с рамкой -> внутренние светлые блики
    # (они «заперты» внутри объекта) автоматически выпадают из маски фона
    n, lab = cv2.connectedComponents(candidate, connectivity=4)
    if n <= 1:
        return candidate.astype(bool)
    border_labels = np.unique(
        np.concatenate([lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]])
    )
    border_labels = border_labels[border_labels != 0]
    return np.isin(lab, border_labels)


def cutout(src: Path, dst: Path, bg: str, padding: float) -> None:
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"Не удалось открыть {src}")
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    background = build_background_mask(rgb, bg)
    obj = (~background).astype(np.uint8)

    # крупнейшая компонента = сам логотип (отсекает пыль и обрезки теней)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(obj, connectivity=8)
    if n > 1:
        idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        obj = (lab == idx).astype(np.uint8)

    # сгладить ступеньки и залить внутренние дырки
    obj = cv2.morphologyEx(obj, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
    contours, _ = cv2.findContours(obj, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(obj)
    cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)
    obj = filled

    alpha = cv2.GaussianBlur(obj * 255, (0, 0), sigmaX=1.2, sigmaY=1.2)

    ys, xs = np.where(alpha > 8)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    side = max(x1 - x0, y1 - y0)
    pad = int(side * padding)
    side += 2 * pad
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    half = side // 2

    # квадратный холст вокруг объекта
    canvas = np.zeros((side, side, 4), np.uint8)
    src_x0, src_y0 = cx - half, cy - half
    dx0, dy0 = max(0, -src_x0), max(0, -src_y0)
    sx0, sy0 = max(0, src_x0), max(0, src_y0)
    sx1 = min(rgb.shape[1], src_x0 + side)
    sy1 = min(rgb.shape[0], src_y0 + side)
    cw, ch = sx1 - sx0, sy1 - sy0
    canvas[dy0:dy0 + ch, dx0:dx0 + cw, :3] = rgb[sy0:sy1, sx0:sx1]
    canvas[dy0:dy0 + ch, dx0:dx0 + cw, 3] = alpha[sy0:sy1, sx0:sx1]

    rgba = cv2.cvtColor(canvas, cv2.COLOR_RGBA2BGRA)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst), rgba)
    print(f"{src.name} -> {dst.name}: {side}x{side}, объект {cw}x{ch}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path)
    p.add_argument("--bg", choices=("auto", "light", "dark"), default="auto")
    p.add_argument("--padding", type=float, default=0.06)
    a = p.parse_args()
    cutout(a.src, a.dst, a.bg, a.padding)


if __name__ == "__main__":
    main()
