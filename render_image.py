"""Картинка с расписанием дня в виде сетки (Pillow).

    render_schedule_image(day, person, rehearsals, generated_at) -> PNG bytes

Рисуется весь день: все аудитории, где в этот день есть репетиции, и все
репетиции. Репетиции человека (по имени или через группу: «весь курс»,
«девушки»…) выделены цветом. Оформление — константы ниже.
"""

from __future__ import annotations

import io
import math
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from names import NameBook, group_of
from schedule_parser import Participant, Rehearsal

# --------------------------------------------------------------------------- #
# Оформление
# --------------------------------------------------------------------------- #

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
FONT_MEDIUM = FONTS_DIR / "Manrope-Medium.ttf"
FONT_BOLD = FONTS_DIR / "Manrope-Bold.ttf"
FONT_EXTRABOLD = FONTS_DIR / "Manrope-ExtraBold.ttf"
FONT_DATE = FONTS_DIR / "PlayfairDisplay-Bold.ttf"

# цвета
BG = "#15121c"
ACCENT = "#f2b24a"            # янтарный: заголовок, плашка имени, мои блоки
TEXT_WHITE = "#ffffff"
TEXT_SOFT = "#c9c2d8"         # день недели, легенда
TEXT_MUTED = "#9d95b0"        # подписи часов, «обновлено»
GRID_LINE = "#3a3347"
MINE_BG = ACCENT
MINE_TITLE = "#15121c"
MINE_TIME = "#15121c"
MINE_PEOPLE = "#3a2a14"
OTHER_BG = "#2a2436"
OTHER_BORDER = "#3a3347"
OTHER_TITLE = "#e0daee"
OTHER_TIME = "#c9c2d8"
OTHER_PEOPLE = "#9d95b0"
PILL_BG = ACCENT
PILL_TEXT = "#15121c"

# размеры шрифтов
SIZE_LABEL = 24               # «РАСПИСАНИЕ РЕПЕТИЦИЙ»
LABEL_TRACKING = 0.28         # разрядка, доля кегля
SIZE_DATE = 100
SIZE_WEEKDAY = 30
SIZE_PILL = 28
SIZE_LEGEND = 22
SIZE_COLUMN = 28
SIZE_HOUR = 22
SIZE_TITLE_MINE = 28
SIZE_TITLE_OTHER = 26
SIZE_TIME = 20
SIZE_PEOPLE = 20
SIZE_MIN = 18                 # меньше не сжимаем, дальше — «…»
SIZE_FOOTER = 22

# геометрия
WIDTH = 1080
PAD_X = 72
PAD_TOP = 72
PAD_BOTTOM = 72
AXIS_W = 84                   # ось времени слева
PX_PER_HOUR = 90
MIN_PX_PER_HOUR = 60
MAX_GRID_H = 1000
GRID_LINE_W = 2
BLOCK_RADIUS = 14
BLOCK_INSET = 3               # отступ блока от часовых линий
BLOCK_PAD_X = 14
BLOCK_PAD_Y = 10
COLUMN_GAP = 6                # отступ блоков от краёв колонки
LANE_GAP = 6                  # между параллельными блоками
ROW_GAP = 4                   # между строками в блоке
PILL_PAD_X = 24
PILL_PAD_Y = 12
PILL_MAX_W = 460
LEGEND_SQUARE = 26

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


# --------------------------------------------------------------------------- #
# Текст
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=None)
def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def text_w(text: str, f: ImageFont.FreeTypeFont) -> float:
    return f.getlength(text)


def fit_text(text: str, path: Path, size: int, max_w: float,
             min_size: int = SIZE_MIN) -> tuple[ImageFont.FreeTypeFont, str]:
    """Уменьшает кегль до min_size, затем сокращает текст с «…»."""
    for s in range(size, min_size - 1, -1):
        f = font(path, s)
        if text_w(text, f) <= max_w:
            return f, text
    f = font(path, min_size)
    if max_w <= text_w("…", f):
        return f, ""
    lo, hi = 0, len(text)
    while lo < hi:                       # самая длинная обрезка, что помещается
        mid = (lo + hi + 1) // 2
        if text_w(text[:mid].rstrip() + "…", f) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return f, text[:lo].rstrip(" ,") + "…"


def draw_tracked(draw: ImageDraw.ImageDraw, xy, text: str, f, fill, tracking: float) -> None:
    x, y = xy
    extra = f.size * tracking
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill, anchor="ls")
        x += text_w(ch, f) + extra


def tracked_w(text: str, f, tracking: float) -> float:
    return sum(text_w(ch, f) for ch in text) + f.size * tracking * max(len(text) - 1, 0)


def people_text(participants: list[Participant]) -> str:
    parts = []
    for p in participants:
        if not p.note:
            parts.append(p.name)
        elif group_of(p) is not None:
            parts.append(f"{p.name} ({p.note})")
        elif p.note == "?":
            parts.append(p.name + "?")
        else:
            parts.append(f"{p.name} [{p.note}]")
    return ", ".join(parts)


def time_label(r: Rehearsal) -> str:
    return f"{r.start:%H:%M}–{r.end:%H:%M}"


# --------------------------------------------------------------------------- #
# Раскладка
# --------------------------------------------------------------------------- #

def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def room_sort_key(room: str):
    return (not room.isdigit(), int(room) if room.isdigit() else 0, room)


def assign_lanes(items: list[Rehearsal]) -> list[tuple[Rehearsal, int, int]]:
    """Параллельные репетиции в одной аудитории: (запись, дорожка, всего дорожек).

    Пересекающиеся по времени записи образуют группу; внутри группы каждой
    записи — своя дорожка, ширина колонки делится на число дорожек группы.
    """
    items = sorted(items, key=lambda r: (_minutes(r.start), _minutes(r.end)))
    result: list[tuple[Rehearsal, int, int]] = []
    cluster: list[tuple[Rehearsal, int]] = []
    lane_ends: list[int] = []
    cluster_end = -1

    def flush():
        n = max((lane for _, lane in cluster), default=-1) + 1
        result.extend((r, lane, n) for r, lane in cluster)

    for r in items:
        start, end = _minutes(r.start), _minutes(r.end)
        if cluster and start >= cluster_end:
            flush()
            cluster, lane_ends = [], []
        lane = next((i for i, e in enumerate(lane_ends) if e <= start), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(end)
        else:
            lane_ends[lane] = end
        cluster.append((r, lane))
        cluster_end = max(cluster_end, end)
    if cluster:
        flush()
    return result


def day_rooms(items: list[Rehearsal]) -> list[str]:
    """Колонки: все аудитории дня по возрастанию номера."""
    return sorted({r.room for r in items}, key=room_sort_key)


def hour_range(items: list[Rehearsal]) -> tuple[int, int]:
    """От начала самого раннего часа до конца самого позднего (в часах)."""
    first = min(_minutes(r.start) for r in items) // 60
    last = math.ceil(max(_minutes(r.end) for r in items) / 60)
    return first, max(last, first + 1)


def px_per_hour(hours: int) -> float:
    if hours * PX_PER_HOUR <= MAX_GRID_H:
        return PX_PER_HOUR
    return max(MIN_PX_PER_HOUR, MAX_GRID_H / hours)


# --------------------------------------------------------------------------- #
# Рисование
# --------------------------------------------------------------------------- #

def _block(w: int, h: int, r: Rehearsal, mine: bool) -> Image.Image:
    """Блок репетиции отдельным слоем: всё, что не влезло, обрезается по краю."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = min(BLOCK_RADIUS, h // 2, w // 2)
    if mine:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=MINE_BG)
        c_title, c_time, c_people = MINE_TITLE, MINE_TIME, MINE_PEOPLE
        title_path, title_size = FONT_EXTRABOLD, SIZE_TITLE_MINE
    else:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=OTHER_BG,
                            outline=OTHER_BORDER, width=1)
        c_title, c_time, c_people = OTHER_TITLE, OTHER_TIME, OTHER_PEOPLE
        title_path, title_size = FONT_BOLD, SIZE_TITLE_OTHER

    inner_w = w - 2 * BLOCK_PAD_X
    if inner_w <= 0:
        return img
    label = time_label(r)
    people = people_text(r.participants) or "—"

    pad_y = BLOCK_PAD_Y
    time_on_row1 = inner_w - text_w(label, font(FONT_BOLD, SIZE_TIME)) - 10 >= 70
    if not time_on_row1:
        # узкий блок (параллельные): время уходит во вторую строку. Чтобы оно
        # не пропало у короткого блока, уменьшаем название и отступы.
        for pad_y in (BLOCK_PAD_Y, 6):
            fit = h - 2 * pad_y - ROW_GAP - SIZE_MIN
            if fit >= SIZE_MIN:
                title_size = min(title_size, fit)
                break
    # кегль первой строки не больше высоты блока (но не меньше минимального)
    title_size = max(SIZE_MIN, min(title_size, h - 2 * pad_y))
    time_f = font(FONT_BOLD, min(SIZE_TIME, title_size))
    title_max = inner_w - (text_w(label, time_f) + 10 if time_on_row1 else 0)
    title_f, title = fit_text(r.title, title_path, title_size, title_max)

    row1_h = title_f.size
    # вторая строка: участники; в узком блоке — время (кегль, под который подобрано название)
    second_h = SIZE_PEOPLE if time_on_row1 else SIZE_MIN
    two_rows = h >= 2 * pad_y + row1_h + ROW_GAP + second_h
    if two_rows:
        y1 = pad_y + row1_h                  # базовая линия первой строки
    else:
        y1 = (h + row1_h * 0.72) / 2         # по центру блока
    d.text((BLOCK_PAD_X, y1), title, font=title_f, fill=c_title, anchor="ls")
    if time_on_row1:
        d.text((w - BLOCK_PAD_X, y1), label, font=time_f, fill=c_time, anchor="rs")
    if two_rows:
        y2 = y1 + ROW_GAP + second_h + 2
        if time_on_row1:
            people_f, people_line = fit_text(people, FONT_MEDIUM, SIZE_PEOPLE, inner_w)
            d.text((BLOCK_PAD_X, y2), people_line, font=people_f, fill=c_people, anchor="ls")
        else:
            # узкий блок (параллельные): время во второй строке целиком,
            # участники — только если после времени осталось место
            t_f, t_line = fit_text(label, FONT_BOLD, second_h, inner_w)
            d.text((BLOCK_PAD_X, y2), t_line, font=t_f, fill=c_time, anchor="ls")
            rest = inner_w - text_w(t_line + " · ", t_f)
            if rest >= 50:
                people_f, people_line = fit_text(people, FONT_MEDIUM, SIZE_PEOPLE, rest)
                d.text((BLOCK_PAD_X + text_w(t_line + " · ", t_f), y2), people_line,
                       font=people_f, fill=c_people, anchor="ls")
    return img


def render_schedule_image(day: date, person: str, rehearsals: list[Rehearsal],
                          generated_at: datetime, book: NameBook | None = None) -> bytes:
    """PNG с сеткой дня `day` для человека `person`.

    rehearsals — можно передать всё расписание: берутся записи только этого дня.
    generated_at — время создания (пишется внизу по Москве).
    """
    book = book or NameBook.load()
    items = [r for r in rehearsals if r.date == day]
    timed = [r for r in items if r.start is not None and r.end is not None and r.end > r.start]
    untimed = [r for r in items if r not in timed]
    rooms = day_rooms(items)

    f_label = font(FONT_EXTRABOLD, SIZE_LABEL)
    f_date = font(FONT_DATE, SIZE_DATE)
    f_weekday = font(FONT_BOLD, SIZE_WEEKDAY)
    f_legend = font(FONT_MEDIUM, SIZE_LEGEND)
    f_column = font(FONT_EXTRABOLD, SIZE_COLUMN)
    f_hour = font(FONT_MEDIUM, SIZE_HOUR)
    f_footer = font(FONT_MEDIUM, SIZE_FOOTER)

    # ---- вертикальная раскладка (считаем заранее, чтобы знать высоту) ----
    y_label = PAD_TOP + SIZE_LABEL                         # базовая линия
    date_text = f"{day:%d.%m.%y}"
    date_box = f_date.getbbox(date_text, anchor="ls")
    y_date = y_label + 24 + (-date_box[1])                 # базовая линия даты
    left_bottom = y_date + date_box[3]

    y_weekday = PAD_TOP + SIZE_WEEKDAY
    pill_f, pill_text = fit_text(person, FONT_EXTRABOLD, SIZE_PILL, PILL_MAX_W - 2 * PILL_PAD_X)
    pill_h = pill_f.size + 2 * PILL_PAD_Y
    pill_top = y_weekday + 20
    right_bottom = pill_top + pill_h

    y_legend = max(left_bottom, right_bottom) + 44
    y_columns = y_legend + LEGEND_SQUARE + 40              # верх заголовков колонок
    grid_top = y_columns + SIZE_COLUMN + 28

    if timed:
        h0, h1 = hour_range(timed)
        pph = px_per_hour(h1 - h0)
        grid_h = round((h1 - h0) * pph)
    else:
        h0 = h1 = 0
        pph = PX_PER_HOUR
        grid_h = 120                                       # место под «репетиций нет»
    grid_bottom = grid_top + grid_h

    untimed_lines = [(f"{r.time_raw or 'время не указано'} · {r.title} · "
                      f"{people_text(r.participants) or '—'}", book.matches(person, r))
                     for r in untimed]
    y_untimed = grid_bottom + 48
    untimed_h = (34 + 30 * len(untimed_lines)) if untimed_lines else 0
    y_footer = y_untimed + untimed_h + (12 if untimed_lines else 0) + SIZE_FOOTER
    height = int(y_footer + PAD_BOTTOM)

    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)

    # ---- шапка ----
    draw_tracked(d, (PAD_X, y_label), "РАСПИСАНИЕ РЕПЕТИЦИЙ", f_label, ACCENT, LABEL_TRACKING)
    d.text((PAD_X, y_date), date_text, font=f_date, fill=TEXT_WHITE, anchor="ls")
    right = WIDTH - PAD_X
    d.text((right, y_weekday), WEEKDAYS[day.weekday()], font=f_weekday, fill=TEXT_SOFT,
           anchor="rs")
    pill_w = text_w(pill_text, pill_f) + 2 * PILL_PAD_X
    d.rounded_rectangle((right - pill_w, pill_top, right, pill_top + pill_h),
                        pill_h / 2, fill=PILL_BG)
    d.text((right - pill_w / 2, pill_top + pill_h / 2), pill_text, font=pill_f,
           fill=PILL_TEXT, anchor="mm")

    # ---- легенда ----
    x = PAD_X
    sq = LEGEND_SQUARE
    for label, fill, outline in (("Мои репетиции", MINE_BG, None),
                                 ("Остальные", OTHER_BG, OTHER_BORDER)):
        d.rounded_rectangle((x, y_legend, x + sq, y_legend + sq), 6, fill=fill,
                            outline=outline, width=2 if outline else 0)
        d.text((x + sq + 14, y_legend + sq / 2), label, font=f_legend, fill=TEXT_SOFT,
               anchor="lm")
        x += sq + 14 + text_w(label, f_legend) + 44

    # ---- колонки и сетка ----
    grid_x0 = PAD_X + AXIS_W
    grid_x1 = WIDTH - PAD_X
    col_w = (grid_x1 - grid_x0) / max(len(rooms), 1)
    for i, room in enumerate(rooms):
        cx = grid_x0 + i * col_w + COLUMN_GAP
        head_f, head = fit_text(f"Ауд. {room}" if room else "Ауд. ?", FONT_EXTRABOLD,
                                SIZE_COLUMN, col_w - 2 * COLUMN_GAP)
        d.text((cx, y_columns + SIZE_COLUMN), head, font=head_f, fill=TEXT_WHITE, anchor="ls")

    if timed:
        for hour in range(h0, h1 + 1):
            y = grid_top + (hour - h0) * pph
            d.line((grid_x0, y, grid_x1, y), fill=GRID_LINE, width=GRID_LINE_W)
            d.text((PAD_X, y), f"{hour:02d}:00", font=f_hour, fill=TEXT_MUTED, anchor="lm")
        for i, room in enumerate(rooms):
            col_x = grid_x0 + i * col_w
            for r, lane, lanes in assign_lanes([t for t in timed if t.room == room]):
                lane_w = (col_w - 2 * COLUMN_GAP - (lanes - 1) * LANE_GAP) / lanes
                bx = col_x + COLUMN_GAP + lane * (lane_w + LANE_GAP)
                by0 = grid_top + (_minutes(r.start) / 60 - h0) * pph + BLOCK_INSET
                by1 = grid_top + (_minutes(r.end) / 60 - h0) * pph - BLOCK_INSET
                w, h = max(int(lane_w), 1), max(int(by1 - by0), 1)
                block = _block(w, h, r, book.matches(person, r))
                img.paste(block, (int(bx), int(by0)), block)
    else:
        d.line((grid_x0, grid_top, grid_x1, grid_top), fill=GRID_LINE, width=GRID_LINE_W)
        d.text((grid_x0, grid_top + 60), "В этот день репетиций нет", font=f_legend,
               fill=TEXT_MUTED, anchor="lm")

    # ---- записи без времени (их нельзя поставить на сетку) ----
    if untimed_lines:
        f_small = font(FONT_MEDIUM, SIZE_PEOPLE)
        d.text((PAD_X, y_untimed + SIZE_PEOPLE), "Без точного времени:", font=f_small,
               fill=TEXT_MUTED, anchor="ls")
        for i, (line, mine) in enumerate(untimed_lines):
            lf, lt = fit_text(line, FONT_MEDIUM, SIZE_PEOPLE, WIDTH - 2 * PAD_X)
            d.text((PAD_X, y_untimed + SIZE_PEOPLE + 34 + i * 30), lt, font=lf,
                   fill=ACCENT if mine else TEXT_SOFT, anchor="ls")

    # ---- низ ----
    stamp = _moscow(generated_at)
    d.text((PAD_X, y_footer), f"обновлено {stamp:%H:%M}", font=f_footer, fill=TEXT_MUTED,
           anchor="ls")

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _moscow(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Moscow"))
    except Exception:
        from datetime import timedelta, timezone
        return dt.astimezone(timezone(timedelta(hours=3)))
