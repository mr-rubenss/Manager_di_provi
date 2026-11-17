"""Логотип бота для профиля Telegram (PNG 1024x1024, Pillow).

    python render_logo.py   -> assets/bot_logo.png

Telegram обрезает аватарку по кругу: всё важное — внутри круга SAFE_RADIUS
от центра (проверяется тестом). Оформление — константы ниже.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
LOGO_FILE = ROOT / "assets" / "bot_logo.png"

SIZE = 1024
BG = "#15121c"
ACCENT = "#f2b24a"
DARK_BLOCK = "#2a2436"
GRID_LINE = "#3a3347"

GROUP_XY = (232, 232)            # левый верх группы
GROUP_SIZE = 560                 # группа GROUP_SIZE x GROUP_SIZE
LINES_Y = (0, 138, 276, 414, 552)
LINE_W = 8
BLOCKS = [                       # (x от, x до, y от, y до, цвет) — внутри группы
    (0, 260, 24, 114, DARK_BLOCK),
    (0, 260, 162, 390, ACCENT),
    (0, 260, 438, 528, DARK_BLOCK),
    (300, 560, 24, 252, DARK_BLOCK),
    (300, 560, 300, 390, DARK_BLOCK),
    (300, 560, 438, 528, ACCENT),
]
SAFE_RADIUS = 380                # блоки должны помещаться в этот круг


def render_logo() -> bytes:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    gx, gy = GROUP_XY
    # линии: верх линии — на смещении (толщина вниз), последняя заканчивается на y=560
    for dy in LINES_Y:
        d.rectangle((gx, gy + dy, gx + GROUP_SIZE - 1, gy + dy + LINE_W - 1), fill=GRID_LINE)
    for x0, x1, y0, y1, color in BLOCKS:
        d.rectangle((gx + x0, gy + y0, gx + x1 - 1, gy + y1 - 1), fill=color)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def block_corners() -> list[tuple[int, int]]:
    """Углы блоков в координатах картинки (для проверки круга)."""
    gx, gy = GROUP_XY
    return [(gx + x, gy + y) for x0, x1, y0, y1, _ in BLOCKS for x in (x0, x1) for y in (y0, y1)]


def main() -> None:
    LOGO_FILE.parent.mkdir(exist_ok=True)
    LOGO_FILE.write_bytes(render_logo())
    print(LOGO_FILE)


if __name__ == "__main__":
    main()
