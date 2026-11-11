"""Снимок расписания и поиск изменений между снимками.

У записей таблицы нет идентификаторов, поэтому сопоставление такое (по дням):
  1. одинаковые записи (время, название, аудитория, состав) — без изменений;
     параллельные репетиции с одинаковыми полями считаются по количеству;
  2. из оставшихся пара «было/стало» ищется сначала по названию, затем по
     времени+аудитории — это «изменение»;
  3. всё, что не нашло пары, — «добавлено» или «удалено».
Перенос на другой день выглядит как «удалено» в старый день и «добавлено» в новый.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

from names import NameBook, normalize
from schedule_parser import Participant, Rehearsal
from storage import write_json

SNAPSHOT_FILE = Path(__file__).resolve().parent / "schedule_snapshot.json"


# --------------------------------------------------------------------------- #
# Снимок
# --------------------------------------------------------------------------- #

def _t(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _pt(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def to_dict(r: Rehearsal) -> dict:
    return {
        "date": r.date.isoformat(), "weekday": r.weekday,
        "start": _t(r.start), "end": _t(r.end), "time_raw": r.time_raw,
        "title": r.title, "room": r.room,
        "participants": [{"name": p.name, "note": p.note} for p in r.participants],
    }


def from_dict(d: dict) -> Rehearsal:
    return Rehearsal(
        date=date.fromisoformat(d["date"]), weekday=d.get("weekday", ""),
        start=_pt(d.get("start")), end=_pt(d.get("end")),
        time_raw=d.get("time_raw", ""), title=d.get("title", ""), room=d.get("room", ""),
        participants=[Participant(p["name"], p.get("note")) for p in d.get("participants", [])],
    )


def upcoming(records: list[Rehearsal], today: date) -> list[Rehearsal]:
    return [r for r in records if r.date is not None and r.date >= today]


def load_snapshot(path: Path = SNAPSHOT_FILE) -> list[Rehearsal] | None:
    """None — снимка ещё нет (первый запуск)."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return [from_dict(d) for d in json.load(f)["records"]]



def save_snapshot(records: list[Rehearsal], path: Path = SNAPSHOT_FILE) -> None:
    write_json(path, {"records": [to_dict(r) for r in records]})


# --------------------------------------------------------------------------- #
# Сравнение
# --------------------------------------------------------------------------- #

@dataclass
class Change:
    day: date
    old: Rehearsal | None  # None — запись добавили
    new: Rehearsal | None  # None — запись удалили


def _signature(r: Rehearsal, book: NameBook) -> tuple:
    """Значимые поля. Регистр, «ё/е», пробелы и порядок участников не важны."""
    when = (_t(r.start), _t(r.end)) if r.start else ("raw", normalize(r.time_raw))
    people = tuple(sorted((book.key(p.name), p.note or "") for p in r.participants))
    return (when, normalize(r.title), r.room.strip(), people)


def diff(old: list[Rehearsal], new: list[Rehearsal], book: NameBook) -> list[Change]:
    changes: list[Change] = []
    for day in sorted({r.date for r in old} | {r.date for r in new}):
        olds = [r for r in old if r.date == day]
        news = [r for r in new if r.date == day]

        # 1. одинаковые записи убираем попарно (параллельные дубликаты — по количеству)
        rest_new = list(news)
        rest_old = []
        for r in olds:
            sig = _signature(r, book)
            twin = next((n for n in rest_new if _signature(n, book) == sig), None)
            if twin is None:
                rest_old.append(r)
            else:
                rest_new.remove(twin)

        # 2. пары «было/стало»: по названию, затем по времени и аудитории
        def same_title(a, b):
            return normalize(a.title) == normalize(b.title)

        def same_slot(a, b):
            return a.start is not None and (a.start, a.end, a.room) == (b.start, b.end, b.room)

        for rule in (same_title, same_slot):
            for r in list(rest_old):
                match = next((n for n in rest_new if rule(r, n)), None)
                if match is not None:
                    changes.append(Change(day, r, match))
                    rest_old.remove(r)
                    rest_new.remove(match)

        # 3. остальное — удалено / добавлено
        changes += [Change(day, r, None) for r in rest_old]
        changes += [Change(day, None, n) for n in rest_new]

    changes.sort(key=lambda c: (c.day, (c.new or c.old).start or time.max))
    return changes


def affected(change: Change, subscriber_name: str, book: NameBook) -> bool:
    """Подписчик есть в старом ИЛИ в новом составе."""
    return any(r is not None and book.matches(subscriber_name, r)
               for r in (change.old, change.new))
