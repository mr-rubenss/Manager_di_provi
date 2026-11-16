"""Баннер объявления об обновлении бота (PNG 1080x1080, Pillow).

    python render_banner.py   -> assets/update_banner.png и out/banner_preview.png

Оформление и тексты — константы ниже (подход как в render_image.py).
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

from render_image import (FONT_BOLD, FONT_EXTRABOLD, FONTS_DIR, draw_tracked, font,
                          text_w)

ROOT = Path(__file__).resolve().parent
BANNER_FILE = ROOT / "assets" / "update_banner.png"
PREVIEW_FILE = ROOT / "out" / "banner_preview.png"
FONT_PIXEL = FONTS_DIR / "PressStart2P-Regular.ttf"

# --------------------------------------------------------------------------- #
# Оформление
# --------------------------------------------------------------------------- #

SIZE = 1080                      # квадрат SIZE x SIZE
MARGIN = 80                      # отступ от краёв (низ, лево, право)

BG = "#15121c"
ACCENT = "#f2b24a"
SHADOW = "#3a3347"
DARK_BLOCK = "#2a2436"
GRID_LINE = "#3a3347"
TEXT_WHITE = "#ffffff"
TEXT_SOFT = "#c9c2d8"
PILL_TEXT = "#15121c"

# надпись сверху
LABEL_TEXT = "ОБНОВЛЕНИЕ БОТА"
LABEL_XY = (80, 88)              # левый верх строки
LABEL_SIZE = 26
LABEL_TRACKING = 0.28

# «UPD»
UPD_TEXT = "UPD"
UPD_XY = (80, 165)               # левый верх строки
UPD_SIZE = 176
UPD_SHADOW_OFFSET = 10           # тень: вправо и вниз

# рисунок сетки справа
GRID_BOX = (660, 88, 1000, 348)  # x0, y0, x1, y1
GRID_LINES_Y = (0, 64, 128, 192, 256)
GRID_LINE_W = 4
GRID_COL_W = 160
GRID_COL2_DX = 180
GRID_BLOCKS = [                  # (колонка 0/1, y от, y до, цвет)
    (0, 8, 56, DARK_BLOCK),
    (0, 72, 184, ACCENT),
    (0, 200, 248, DARK_BLOCK),
    (1, 40, 120, DARK_BLOCK),
    (1, 136, 184, DARK_BLOCK),
    (1, 200, 248, ACCENT),
]

# три строки с командами
ROWS = [
    ("/now", "что идёт прямо сейчас"),
    ("/who", "расписание друга"),
    ("/room", "репетиции в аудитории"),
]
ROW_FIRST_Y = 410                # верх рамки первой строки
ROW_STEP = 106
ROW_BOX_W = 220
ROW_BOX_H = 76
ROW_BOX_BORDER = 3
ROW_GAP = 32                     # от рамки до описания
CMD_SIZE = 36
DESC_SIZE = 38

# низ
PILL_LABEL = "Ещё команды и картинка-сетка"
PILL_SIZE = 30
PILL_PAD_X = 34
PILL_PAD_Y = 18
NOTE_TEXT = "читайте в сообщении ниже"
NOTE_SIZE = 28
NOTE_GAP = 24                    # от плашки до текста справа
NOTE_MIN_SIZE = 20               # если не помещается до правого поля — уменьшаем


# --------------------------------------------------------------------------- #

def render_banner() -> bytes:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)

    # надпись: draw_tracked рисует от базовой линии, переводим «верх» в базовую линию
    f_label = font(FONT_EXTRABOLD, LABEL_SIZE)
    ascent = f_label.getmetrics()[0]
    draw_tracked(d, (LABEL_XY[0], LABEL_XY[1] + ascent), LABEL_TEXT, f_label, ACCENT,
                 LABEL_TRACKING)

    # UPD с тенью
    f_upd = font(FONT_PIXEL, UPD_SIZE)
    x, y = UPD_XY
    d.text((x + UPD_SHADOW_OFFSET, y + UPD_SHADOW_OFFSET), UPD_TEXT, font=f_upd,
           fill=SHADOW, anchor="la")
    d.text((x, y), UPD_TEXT, font=f_upd, fill=ACCENT, anchor="la")

    # рисунок сетки: сначала линии, поверх — блоки
    gx0, gy0, gx1, _ = GRID_BOX
    for dy in GRID_LINES_Y:
        top = gy0 + dy - GRID_LINE_W // 2
        d.rectangle((gx0, top, gx1, top + GRID_LINE_W - 1), fill=GRID_LINE)
    for col, y_from, y_to, color in GRID_BLOCKS:
        bx = gx0 + col * GRID_COL2_DX
        d.rectangle((bx, gy0 + y_from, bx + GRID_COL_W - 1, gy0 + y_to - 1), fill=color)

    # строки команд
    f_cmd = font(FONT_EXTRABOLD, CMD_SIZE)
    f_desc = font(FONT_BOLD, DESC_SIZE)
    for i, (cmd, desc) in enumerate(ROWS):
        top = ROW_FIRST_Y + i * ROW_STEP
        box = (MARGIN, top, MARGIN + ROW_BOX_W - 1, top + ROW_BOX_H - 1)
        d.rectangle(box, outline=ACCENT, width=ROW_BOX_BORDER)
        cy = top + ROW_BOX_H / 2
        d.text((MARGIN + ROW_BOX_W / 2, cy), cmd, font=f_cmd, fill=ACCENT, anchor="mm")
        d.text((MARGIN + ROW_BOX_W + ROW_GAP, cy), desc, font=f_desc, fill=TEXT_WHITE,
               anchor="lm")

    # плашка внизу и подпись справа
    f_pill = font(FONT_EXTRABOLD, PILL_SIZE)
    asc, desc_ = f_pill.getmetrics()
    pill_w = text_w(PILL_LABEL, f_pill) + 2 * PILL_PAD_X
    pill_h = PILL_SIZE + 2 * PILL_PAD_Y
    bottom = SIZE - MARGIN
    pill = (MARGIN, bottom - pill_h, MARGIN + pill_w, bottom)
    d.rectangle(pill, fill=ACCENT)
    cy = bottom - pill_h / 2
    d.text((MARGIN + pill_w / 2, cy), PILL_LABEL, font=f_pill, fill=PILL_TEXT, anchor="mm")

    note_x = MARGIN + pill_w + NOTE_GAP
    room = SIZE - MARGIN - note_x
    size = NOTE_SIZE
    while size > NOTE_MIN_SIZE and text_w(NOTE_TEXT, font(FONT_BOLD, size)) > room:
        size -= 1
    d.text((note_x, cy), NOTE_TEXT, font=font(FONT_BOLD, size), fill=TEXT_SOFT, anchor="lm")

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def note_fits() -> tuple[bool, int]:
    """Помещается ли подпись справа от плашки кеглем NOTE_SIZE (для проверки макета)."""
    f_pill = font(FONT_EXTRABOLD, PILL_SIZE)
    note_x = MARGIN + text_w(PILL_LABEL, f_pill) + 2 * PILL_PAD_X + NOTE_GAP
    need = text_w(NOTE_TEXT, font(FONT_BOLD, NOTE_SIZE))
    return note_x + need <= SIZE - MARGIN, int(note_x + need)


def main() -> None:
    png = render_banner()
    for path in (BANNER_FILE, PREVIEW_FILE):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(png)
        print(path)


if __name__ == "__main__":
    main()
