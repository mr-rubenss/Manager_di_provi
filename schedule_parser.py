"""Загрузка и разбор расписания репетиций из Google Таблицы.

Структура вкладки:
  * строка 1 — день недели и дата («понедельник 16.11.26»); ячейка стоит над
    первым столбцом дня, остальные столбцы дня пустые;
  * строка 2 — номер аудитории над каждым блоком из 3 столбцов
    (в один день может быть несколько аудиторий);
  * строка 3 — заголовки «время / название / участники»;
  * с 4-й строки — данные.

Таблица только читается (CSV-экспорт), ничего в ней не меняется.

Запуск: ``python schedule_parser.py`` — скачает «Лист1» и напечатает записи.
"""

from __future__ import annotations

import csv
import io
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, time

# Таблица и рабочая вкладка. Можно переопределить переменными окружения
# (в GitHub — Settings → Secrets and variables → Actions → Variables).
SPREADSHEET_ID = (os.environ.get("SCHEDULE_SPREADSHEET_ID")
                  or "1kVfvZjTg28-gBQXUATRMetdhLhUGdL0dg4oxJSJ2fdw")
SHEET_NAME = os.environ.get("SCHEDULE_SHEET_NAME") or "Лист1"

BLOCK_WIDTH = 3   # время, название, участники
DATA_START_ROW = 3  # индекс (с нуля) первой строки с данными
TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Модель
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Participant:
    name: str
    note: str | None = None  # пометка из скобок: «(Василиса?)» -> note="?"

    def __str__(self) -> str:
        return f"{self.name} ({self.note})" if self.note else self.name


@dataclass
class Rehearsal:
    date: date | None
    weekday: str
    start: time | None
    end: time | None
    time_raw: str
    title: str
    room: str
    participants: list[Participant] = field(default_factory=list)

    @property
    def time_str(self) -> str:
        if self.start is None:
            return self.time_raw or "—"
        s = self.start.strftime("%H:%M")
        return f"{s}–{self.end.strftime('%H:%M')}" if self.end else s


# --------------------------------------------------------------------------- #
# Загрузка
# --------------------------------------------------------------------------- #

def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "schedule-bot/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def resolve_gid(spreadsheet_id: str, sheet_name: str) -> str:
    """Находит gid вкладки по её названию (через публичный htmlview)."""
    html = _get(f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/htmlview")
    for name, gid in re.findall(r'name:\s*"([^"]*)".*?gid:\s*"(-?\d+)"', html):
        if name.strip() == sheet_name.strip():
            return gid
    raise LookupError(f"Вкладка «{sheet_name}» не найдена в таблице")


def fetch_csv(spreadsheet_id: str = SPREADSHEET_ID, sheet_name: str = SHEET_NAME,
              gid: str | None = None) -> str:
    """Скачивает вкладку как CSV. Используется export, а не gviz: gviz
    склеивает/теряет строки шапки."""
    if gid is None:
        gid = resolve_gid(spreadsheet_id, sheet_name)
    query = urllib.parse.urlencode({"format": "csv", "gid": gid})
    return _get(f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?{query}")


# --------------------------------------------------------------------------- #
# Разбор отдельных ячеек
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")


def _clean(s: str | None) -> str:
    return _WS.sub(" ", (s or "").replace("\xa0", " ")).strip()


def parse_day_header(cell: str) -> tuple[str, date | None]:
    """«понедельник 16.11.26» -> ("понедельник", date(2026, 11, 16))."""
    cell = _clean(cell)
    m = _DATE.search(cell)
    if not m:
        return cell, None
    d, mth, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    weekday = _clean(cell[:m.start()] + cell[m.end():]).strip(",;").lower()
    try:
        return weekday, date(y, mth, d)
    except ValueError:
        return weekday, None


_NUM = re.compile(r"\d{1,2}")


def _mk(h: int, m: int = 0) -> time | None:
    return time(h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def parse_time_range(raw: str) -> tuple[time | None, time | None]:
    """Разбирает интервал времени в разных «кривых» записях.

    «10-11» -> 10:00–11:00, «14:30 - 15:30» -> 14:30–15:30,
    «16:15-16-35» -> 16:15–16:35, «16-35» -> 16:35, «15:30- 16:30», «19:00–20:00».

    Если запись нельзя понять однозначно (неверные часы/минуты, конец раньше
    начала, несколько интервалов, нет начала), возвращается (None, None):
    бот не угадывает и показывает текст ячейки как есть.
    """
    s = _clean(raw)
    if not s:
        return None, None
    s = re.sub(r"[‐-―−]", "-", s)  # все виды тире -> «-»
    nums = [(int(m.group()), m.start(), m.end()) for m in _NUM.finditer(s)]
    if not nums or s[:nums[0][1]].strip().endswith("-"):  # «-11:00»: начала нет
        return None, None
    # разделители между соседними числами
    seps = [s[a[2]:b[1]].strip() or " " for a, b in zip(nums, nums[1:])]
    n = [x[0] for x in nums]

    def is_minsep(sep: str) -> bool:
        return sep in (":", ".", ",")

    ok = True

    def mk(h: int, m: int = 0) -> time | None:
        nonlocal ok
        t = _mk(h, m)
        ok = ok and t is not None
        return t

    if len(n) > 4:                        # «12:00-13:00, 14-15» — два интервала
        return None, None
    if len(n) == 4:
        start, end = mk(n[0], n[1]), mk(n[2], n[3])
    elif len(n) == 3:
        if is_minsep(seps[0]):            # 10:30-11
            start, end = mk(n[0], n[1]), mk(n[2])
        else:                             # 10-11:30
            start, end = mk(n[0]), mk(n[1], n[2])
    elif len(n) == 2:
        a, b = n
        if is_minsep(seps[0]):            # 16:35
            start, end = mk(a, b), None
        elif b > 23:                      # «16-35» — опечатка вместо 16:35
            start, end = mk(a, b), None
        else:                             # «10-11»
            start, end = mk(a), mk(b)
    else:
        start, end = mk(n[0]), None

    if not ok or (end is not None and end <= start):
        return None, None
    return start, end


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    """Делит по запятым, не трогая запятые внутри скобок."""
    parts, buf, depth = [], [], 0
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _outside_parens(s: str, fn) -> str:
    """Применяет fn только к тексту вне скобок."""
    out, buf, depth = [], [], 0
    for ch in s:
        if ch in "([":
            if depth == 0:
                out.append(fn("".join(buf)))
                buf = []
            depth += 1
            out.append(ch)
        elif ch in ")]" and depth:
            depth -= 1
            out.append(ch)
        elif depth:
            out.append(ch)
        else:
            buf.append(ch)
    out.append(fn("".join(buf)))
    return "".join(out)


_PARENS = re.compile(r"[(\[]([^)\]]*)[)\]]?")


def parse_participants(raw: str) -> list[Participant]:
    """«Софа, Даня, (Василиса?)» -> [Софа, Даня, Василиса(note="?")]."""
    result = []
    # перенос строки в ячейке, «;» и союз «и» — тоже разделители (но не в скобках)
    raw = _outside_parens(raw or "", lambda t: re.sub(r"\s+и\s+", ",", re.sub(r"[\r\n;]+", ",", t)))
    for part in _split_top_level(_clean(raw)):
        part = _clean(part)
        if not part:
            continue
        notes = [_clean(x) for x in _PARENS.findall(part)]
        name = _clean(_PARENS.sub(" ", part).replace(")", " ").replace("]", " ")).strip(" ,-")
        if not name:
            # вся запись в скобках: «(Василиса?)» — имя под вопросом
            inner = " ".join(notes)
            name = inner.rstrip("?!").strip()
            note = "?" if inner.endswith("?") else None
            if not name:
                continue
        else:
            note = "; ".join(x for x in notes if x) or None
        result.append(Participant(name, note))
    return result


# --------------------------------------------------------------------------- #
# Разбор вкладки
# --------------------------------------------------------------------------- #

def parse_rows(rows: list[list[str]]) -> list[Rehearsal]:
    if len(rows) < DATA_START_ROW:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    day_row, room_row = rows[0], rows[1]

    # начала дней — непустые ячейки первой строки
    day_starts = [i for i, c in enumerate(day_row) if _clean(c)]
    records: list[Rehearsal] = []

    for k, start_col in enumerate(day_starts):
        end_col = day_starts[k + 1] if k + 1 < len(day_starts) else width
        weekday, day_date = parse_day_header(day_row[start_col])

        for col in range(start_col, end_col, BLOCK_WIDTH):
            room = next((_clean(c) for c in room_row[col:col + BLOCK_WIDTH] if _clean(c)), "")
            for row in rows[DATA_START_ROW:]:
                cells = (row[col:col + BLOCK_WIDTH] + [""] * BLOCK_WIDTH)[:BLOCK_WIDTH]
                t_raw, title = _clean(cells[0]), _clean(cells[1])
                people = cells[2] or ""  # без _clean: перенос строки — тоже разделитель
                if not (title or _clean(people)):
                    continue  # пустая строка или одиноко стоящее время
                start, end = parse_time_range(t_raw)
                records.append(Rehearsal(
                    date=day_date, weekday=weekday, start=start, end=end,
                    time_raw=t_raw, title=title, room=room,
                    participants=parse_participants(people),
                ))

    records.sort(key=lambda r: (r.date or date.max, r.start or time.max, r.room))
    return records


def parse_csv(text: str) -> list[Rehearsal]:
    return parse_rows(list(csv.reader(io.StringIO(text))))


def load_schedule(spreadsheet_id: str = SPREADSHEET_ID,
                  sheet_name: str = SHEET_NAME) -> list[Rehearsal]:
    return parse_csv(fetch_csv(spreadsheet_id, sheet_name))


if __name__ == "__main__":
    current = None
    for r in load_schedule():
        if r.date != current:
            current = r.date
            print(f"\n=== {r.weekday} {r.date:%d.%m.%Y} ===" if r.date else "\n=== без даты ===")
        people = ", ".join(map(str, r.participants)) or "—"
        print(f"  {r.time_str:<12} ауд. {r.room:<4} {r.title} | {people}")
