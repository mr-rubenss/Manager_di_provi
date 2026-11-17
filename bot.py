"""Telegram-бот расписания репетиций. Работает «по вызову» (GitHub Actions).

Режимы:
  python bot.py poll                 — обработать новые сообщения (getUpdates),
                                       затем проверить изменения в таблице
  python bot.py send_daily [--force] — разослать подписчикам репетиции на завтра;
                                       без --force только с 21:00 МСК и раз в сутки
  python bot.py run                  — poll + send_daily, если наступил час рассылки
                                       и она ещё не сделана (запасной путь для poll.yml)
  python bot.py check_names          — показать имена, которые нужно проверить
  python bot.py preview ИМЯ [дд.мм.гг] — показать, что придёт человеку (по умолчанию — завтра)
  python bot.py image ИМЯ [дд.мм.гг]   — нарисовать картинку дня в out/preview.png
  python bot.py setup_profile [--apply] — имя, описания и меню команд бота
                                       (без --apply — только показать)
  python bot.py broadcast [--send] [--force] — объявление из broadcast_message.txt всем
                                       (без --send — только проверка)

Локальная проверка без отправки сообщений (токен не нужен):
  python bot.py run --dry-run [--now "2026-11-11 21:05"] [--updates examples/dry_run_updates.json]
  --dry-run  Telegram не вызывается: сообщения печатаются, а JSON-файлы
             меняются только во временной копии;
  --updates  «входящие сообщения» из файла вместо getUpdates;
  --now      подменить текущее время (МСК), например чтобы проверить 21:00.

Токен берётся только из переменной окружения TELEGRAM_BOT_TOKEN и нигде
не печатается: ошибки сети очищаются от URL с токеном.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

import changes as ch
from names import NameBook, group_of, normalize
from schedule_parser import Participant, Rehearsal, load_schedule
from storage import read_json, write_json

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
SUBSCRIBERS_FILE = ROOT / "subscribers.json"
OUT_DIR = ROOT / "out"            # картинки для проверки (в .gitignore)

TZ_NAME = os.environ.get("BOT_TIMEZONE", "Europe/Moscow")
DAILY_HOUR = int(os.environ.get("DAILY_HOUR", "21"))  # рассылка — с 21:00 по BOT_TIMEZONE

CAPTION_LIMIT = 1000   # Telegram: подпись к фото до 1024 символов; берём с запасом
BUTTONS_PER_ROW = 3
AWAIT_WHO_TTL = timedelta(hours=24)  # сколько ждать имя после /who без имени
NOTHING_AT_TIME = "В это время репетиций нет"

# Рабочий день аудиторий для /free (по МСК); можно переопределить переменными окружения
def _workday(var: str, default: str) -> str:
    value = (os.environ.get(var) or "").strip()
    if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
        return value.zfill(5)
    return default        # пусто или неверный формат — значение по умолчанию


WORKDAY_START = _workday("WORKDAY_START", "09:00")
WORKDAY_END = _workday("WORKDAY_END", "22:00")
FIND_LIMIT = 10  # сколько ближайших репетиций показывает /find

# Единый список команд: из него строятся /help и меню команд Telegram (setMyCommands).
# (команда, аргументы для /help, описание)
COMMANDS = [
    ("start", "", "что умеет бот и как сменить имя"),
    ("today", "", "мои репетиции сегодня"),
    ("tomorrow", "", "мои репетиции завтра"),
    ("week", "", "мои репетиции на 7 дней"),
    ("date", "дд.мм", "мои репетиции на дату"),
    ("next", "", "моя ближайшая репетиция"),
    ("now", "", "какие репетиции идут сейчас"),
    ("at", "ЧЧ:ММ [дд.мм]", "что идёт в указанное время"),
    ("who", "[имя]", "расписание человека на сегодня и завтра"),
    ("room", "[номер] [дд.мм]", "репетиции в аудитории"),
    ("free", "[аудитория] [дд.мм]", "свободные окна аудитории"),
    ("find", "название", "ближайшие репетиции отрывка"),
    ("mute", "", "выключить вечернюю рассылку и уведомления"),
    ("unmute", "", "включить вечернюю рассылку и уведомления"),
    ("stop", "", "отписаться и удалить мои данные"),
    ("help", "", "список команд"),
]
HELP_TEXT = "Команды:\n" + "\n".join(
    f"/{cmd}{' ' + args if args else ''} — {desc}" for cmd, args, desc in COMMANDS)
TELEGRAM_COMMANDS = [{"command": cmd, "description": desc} for cmd, _, desc in COMMANDS]


ABOUT_TEXT = (
    "Привет! Я присылаю расписание репетиций из общей таблицы.\n\n"
    "Что я умею:\n"
    "• каждый вечер после 21:00 присылаю твои репетиции на завтра;\n"
    "• сообщаю, если в расписании что-то поменялось;\n"
    "• показываю твои репетиции по командам;\n"
    "• подскажу, что идёт сейчас, где чья репетиция и что в аудитории.\n\n"
    "Отвечаю не мгновенно: обычно в течение 5–60 минут."
)


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Telegram API
# --------------------------------------------------------------------------- #

class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class TelegramAPI:
    def __init__(self, token: str, session: requests.Session | None = None,
                 timeout: float = 30):
        if not token:
            raise TelegramError("Не задан TELEGRAM_BOT_TOKEN")
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}/"
        self._session = session or requests.Session()
        self._timeout = timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<TOKEN>")

    def __repr__(self) -> str:
        return "TelegramAPI(<TOKEN>)"

    def call(self, method: str, **params):
        return self._post(method, json=params)

    def send_photo(self, chat_id, png: bytes, filename: str = "schedule.png",
                   caption: str | None = None):
        """Картинка (multipart/form-data), подпись — обычный текст без parse_mode."""
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        return self._post("sendPhoto", data=data,
                          files={"photo": (filename, png, "image/png")})

    def _post(self, method: str, **request):
        error = None
        try:
            resp = self._session.post(self._base + method, timeout=self._timeout, **request)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            # текст исключения requests содержит URL с токеном: чистим его и
            # бросаем новую ошибку вне except, чтобы исходная не попала в traceback
            error = self._redact(f"{method}: {type(e).__name__}: {e}")
        if error is not None:
            raise TelegramError(error)
        if not data.get("ok"):
            raise TelegramError(self._redact(f"{method}: {data.get('description')}"),
                                data.get("error_code"))
        return data["result"]

    def send_message(self, chat_id, text: str, keyboard: list | None = None):
        params = {"chat_id": chat_id, "text": text}
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)


# --------------------------------------------------------------------------- #
# Хранилище (JSON-файлы в репозитории)
# --------------------------------------------------------------------------- #

class Store:
    """state.json и subscribers.json.

    В subscribers.json значение — имя («Соня») или, если человек выключил
    рассылку командой /mute, объект {"name": "Соня", "muted": true}.
    В памяти: subscribers — chat_id -> имя, muted — множество chat_id.
    """

    def __init__(self, state_file: Path = STATE_FILE,
                 subscribers_file: Path = SUBSCRIBERS_FILE):
        self.state_file = state_file
        self.subscribers_file = subscribers_file
        self.state = {"offset": 0, "last_daily": None} | read_json(state_file, {})
        self.subscribers: dict[str, str] = {}
        self.muted: set[str] = set()
        for chat, value in read_json(subscribers_file, {}).items():
            if isinstance(value, dict):
                self.subscribers[chat] = value.get("name", "")
                if value.get("muted"):
                    self.muted.add(chat)
            else:
                self.subscribers[chat] = value

    def is_muted(self, chat_id) -> bool:
        return str(chat_id) in self.muted

    def save(self) -> None:
        self.muted &= set(self.subscribers)  # /stop удаляет и флаг
        data = {chat: ({"name": name, "muted": True} if chat in self.muted else name)
                for chat, name in self.subscribers.items()}
        write_json(self.state_file, self.state)
        write_json(self.subscribers_file, data)


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #

def format_time(r: Rehearsal) -> str:
    if r.start and r.end:
        return f"{r.start:%H:%M}-{r.end:%H:%M}"
    if r.start:
        return f"{r.start:%H:%M}"
    return r.time_raw or "время не указано"


def format_participant(p: Participant) -> str:
    if not p.note:
        return p.name
    if group_of(p) is not None:
        return f"{p.name} ({p.note})"  # группа — как в таблице: «хор (женщины)»
    return p.name + "?" if p.note == "?" else f"{p.name} [{p.note}]"


def format_line(r: Rehearsal) -> str:
    people = ", ".join(format_participant(p) for p in r.participants) or "—"
    return (f"{format_time(r)} {r.title} "
            f"(задействованы: {people}; аудитория {r.room or 'не указана'})")


def _by_time(r: Rehearsal):
    return (r.start is None, r.start or datetime.min.time())


def format_day(day: date, rehearsals: list[Rehearsal]) -> str:
    # sorted() стабилен: параллельные репетиции в одно время остаются
    # отдельными строками в порядке таблицы
    items = sorted(rehearsals, key=_by_time)
    return "\n".join([f"РАСПИСАНИЕ РЕПЕТИЦИЙ {day:%d.%m.%y}:"] + [format_line(r) for r in items])


def format_changes(changes: list["ch.Change"]) -> str:
    """Все изменения одного человека — одним сообщением, блоками по датам."""
    blocks = []
    for day in sorted({c.day for c in changes}):
        lines = [f"ИЗМЕНЕНИЕ В РАСПИСАНИИ {day:%d.%m.%y}:"]
        for c in (c for c in changes if c.day == day):
            lines.append("было: " + (format_line(c.old) if c.old else "—"))
            lines.append("стало: " + (format_line(c.new) if c.new else "отменено"))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram принимает до 4096 символов: режем по абзацам, затем по строкам."""
    parts, cur = [], ""
    for line in text.split("\n"):
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit and cur:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def format_block(header: str, items: list[Rehearsal], empty: str = "репетиций нет") -> str:
    """Заголовок и строки как в расписании (по времени); пусто — строка `empty`."""
    lines = [format_line(r) for r in sorted(items, key=_by_time)] or [empty]
    return "\n".join([header] + lines)


def running_at(schedule: list[Rehearsal], day: date, at: time) -> list[Rehearsal]:
    """Репетиции, которые идут в момент `at`: начало <= at < конец.

    Записи без времени конца сюда не попадают — бот не угадывает длительность.
    """
    return [r for r in schedule
            if r.date == day and r.start is not None and r.end is not None
            and r.start <= at < r.end]


_HHMM = re.compile(r"(\d{1,2})[:.](\d{2})")
_DDMM = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?")


def parse_hhmm(token: str) -> time | None:
    m = _HHMM.fullmatch(token.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return time(h, mi) if h <= 23 and mi <= 59 else None


def parse_ddmm(token: str, today: date) -> date | None:
    """«17.11», «17.11.26», «17.11.2026»; без года — текущий год."""
    m = _DDMM.fullmatch(token.strip())
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y = m.group(3)
    year = today.year if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
    try:
        return date(year, mo, d)
    except ValueError:
        return None


def is_running(r: Rehearsal, now: datetime) -> bool:
    return (r.date == now.date() and r.start is not None and r.end is not None
            and r.start <= now.time() < r.end)


def ended(r: Rehearsal, now: datetime) -> bool:
    """Репетиция уже прошла. Сегодняшняя без времени считается непрошедшей."""
    if r.date is None or r.date < now.date():
        return True
    if r.date > now.date():
        return False
    last = r.end or r.start
    return last is not None and last <= now.time()


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def free_windows(room: str, day: date, schedule: list[Rehearsal],
                 start: str = None, end: str = None) -> tuple[list[tuple[time, time]], list[Rehearsal]]:
    """Свободные окна аудитории в рабочий день и записи без времени окончания
    (их длительность неизвестна — они в расчёт не берутся и показываются отдельно)."""
    day_start, day_end = _hhmm(start or WORKDAY_START), _hhmm(end or WORKDAY_END)
    items = [r for r in schedule if r.date == day and r.room == room]
    busy = sorted((max(r.start, day_start), min(r.end, day_end)) for r in items
                  if r.start is not None and r.end is not None
                  and r.end > day_start and r.start < day_end)
    unclear = [r for r in items if r.start is None or r.end is None]
    windows, cursor = [], day_start
    for b_start, b_end in busy:
        if b_start > cursor:
            windows.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if cursor < day_end:
        windows.append((cursor, day_end))
    return windows, unclear


def format_free(room: str, day: date, schedule: list[Rehearsal]) -> str:
    windows, unclear = free_windows(room, day, schedule)
    lines = [f"СВОБОДНО {room} {day:%d.%m.%y} ({WORKDAY_START}–{WORKDAY_END}):"]
    lines += [f"{a:%H:%M}-{b:%H:%M}" for a, b in windows] or ["свободных окон нет"]
    if unclear:
        lines.append("не учтены — нет времени окончания:")
        lines += [format_line(r) for r in unclear]
    return "\n".join(lines)


def tg_len(text: str) -> int:
    """Длина так, как её считает Telegram (UTF-16): эмодзи — 2 символа."""
    return len(text.encode("utf-16-le")) // 2


def keyboard(buttons: list[dict]) -> list[list[dict]]:
    buttons = [b for b in buttons if len(b["callback_data"].encode()) <= 64]
    return [buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]


def rehearsals_for(name: str, day: date, schedule: list[Rehearsal],
                   book: NameBook) -> list[Rehearsal]:
    return [r for r in schedule if r.date == day and book.matches(name, r)]


# --------------------------------------------------------------------------- #
# Логика бота
# --------------------------------------------------------------------------- #

def local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:  # нет базы часовых поясов — считаем МСК (UTC+3)
        return datetime.now(timezone(timedelta(hours=3)))


class Bot:
    def __init__(self, api: TelegramAPI, store: Store, book: NameBook,
                 schedule_loader: Callable[[], list[Rehearsal]] = load_schedule,
                 now: Callable[[], datetime] = local_now,
                 daily_hour: int = DAILY_HOUR,
                 snapshot_file: Path = ch.SNAPSHOT_FILE):
        self.api = api
        self.store = store
        self.book = book
        self._loader = schedule_loader
        self._schedule: list[Rehearsal] | None = None
        self.now = now
        self.daily_hour = daily_hour
        self.snapshot_file = snapshot_file
        self._request_now: datetime | None = None

    def today(self) -> date:
        return self.now().date()

    def request_now(self) -> datetime:
        """Время, когда человек отправил сообщение (по МСК), иначе — текущее.

        Бот отвечает с задержкой, а «сейчас» и «сегодня» должны быть моментом
        вопроса, а не моментом ответа.
        """
        return self._request_now or self.now()

    @property
    def schedule(self) -> list[Rehearsal]:
        if self._schedule is None:
            self._schedule = self._loader()
        return self._schedule

    # -- poll ------------------------------------------------------------ #

    def sync_commands(self) -> None:
        """Меню команд в Telegram (setMyCommands) — только если список изменился."""
        digest = hashlib.sha256(json.dumps(TELEGRAM_COMMANDS, ensure_ascii=False)
                                .encode()).hexdigest()[:16]
        if self.store.state.get("commands_hash") == digest:
            return
        try:
            self.api.call("setMyCommands", commands=TELEGRAM_COMMANDS)
        except TelegramError as e:
            log(f"Не удалось обновить меню команд: {e}")   # попробуем в следующий раз
            return
        self.store.state["commands_hash"] = digest
        self.store.save()
        log("Меню команд в Telegram обновлено")

    def poll(self) -> int:
        handled = 0
        self.sync_commands()
        if self.store.state.get("awaiting_who"):
            self.cleanup_awaiting()      # chat_id не держим в state.json дольше суток
            self.store.save()
        while True:
            updates = self.api.call("getUpdates", offset=self.store.state["offset"],
                                    timeout=0, limit=100,
                                    allowed_updates=["message", "callback_query"])
            if not updates:
                break
            for upd in updates:
                try:
                    self.handle_update(upd)
                except Exception as e:  # одно сломанное сообщение не блокирует очередь
                    log(f"Ошибка обработки update: {type(e).__name__}: {e}")
                self.store.state["offset"] = upd["update_id"] + 1
                handled += 1
            self.store.save()
        return handled

    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            self.handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not text:
            return
        chat_id = chat["id"]
        self._request_now = self._message_time(msg)
        command = text.split()[0].split("@")[0].lower()
        args = text.split()[1:]
        awaiting_who = self._pop_awaiting_who(chat_id)  # любое сообщение снимает ожидание

        if not text.startswith("/"):
            if awaiting_who:
                self.send_who(chat_id, text)
            else:
                self.handle_name(chat_id, text)
        elif command == "/now":
            now = self.request_now()
            self.send_at(chat_id, now.date(), now.time().replace(second=0, microsecond=0))
        elif command == "/at":
            self.handle_at(chat_id, args)
        elif command == "/who":
            if args:
                self.send_who(chat_id, " ".join(args))
            else:
                self.ask_who(chat_id)
        elif command == "/room":
            self.handle_room(chat_id, args)
        elif command == "/start":
            self.send_welcome(chat_id)
        elif command == "/stop":
            if self.store.subscribers.pop(str(chat_id), None) is not None:
                self.store.save()
                self.api.send_message(chat_id, "Твои данные удалены. "
                                      "Чтобы подписаться снова, отправь /start.")
            else:
                self.api.send_message(chat_id, "Тебя нет среди подписчиков, удалять нечего.")
        elif command in ("/today", "/tomorrow", "/week"):
            self.send_personal(chat_id, command)
        elif command == "/date":
            self.handle_date(chat_id, args)
        elif command == "/next":
            self.send_next(chat_id)
        elif command == "/find":
            self.send_find(chat_id, " ".join(args))
        elif command == "/free":
            self.handle_free(chat_id, args)
        elif command in ("/mute", "/unmute"):
            self.set_muted(chat_id, command == "/mute")
        else:                                            # /help и неизвестные команды
            self.api.send_message(chat_id, HELP_TEXT)

    def handle_callback(self, cq: dict) -> None:
        try:  # бот отвечает с задержкой — старый callback Telegram может уже не принять
            self.api.call("answerCallbackQuery", callback_query_id=cq["id"])
        except TelegramError:
            pass
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id = chat["id"]
        data = cq.get("data") or ""

        self._request_now = None
        if data.startswith("w:"):                       # /who: выбран человек
            self._pop_awaiting_who(chat_id)
            name = self.book.find_registration_name(self.schedule, data[2:])
            if name is None:
                self.api.send_message(chat_id, "Это имя больше не найдено. Попробуй /who ещё раз.")
            else:
                self.send_who_schedule(chat_id, name)
            return
        if data.startswith("r:"):                       # /room: выбрана аудитория
            room, _, day = data[2:].partition(":")
            self.send_room(chat_id, room, [date.fromisoformat(day)] if day else None)
            return

        # Кнопки остались только в старых сообщениях: теперь имя пишут текстом.
        if data == "agree":
            self.send_welcome(chat_id)
        elif data.startswith("n:"):
            name = self.book.find_registration_name(self.schedule, data[2:])
            if name is None:
                self.api.send_message(chat_id, "Это имя больше не найдено в расписании. "
                                      "Отправь /start ещё раз.")
                return
            self.store.subscribers[str(chat_id)] = name
            self.store.save()
            self.api.send_message(chat_id, f"Готово, ты записан(а) как «{name}». "
                                  f"Каждый вечер пришлю репетиции на завтра.\n\n{HELP_TEXT}")

    def welcome_text(self, chat_id) -> str:
        examples = "; ".join(" или ".join(g) for g in self.book.repeated_names(self.schedule))
        parts = [ABOUT_TEXT, HELP_TEXT]
        current = self.store.subscribers.get(str(chat_id))
        ask = ("Чтобы начать, напиши своё имя так, как оно записано в расписании, "
               "например: Соня.")
        if current:
            ask = (f"Сейчас ты записан(а) как «{current}». "
                   f"Чтобы сменить имя, просто напиши новое.")
        if examples:
            ask += ("\nЕсли твоё имя есть у нескольких человек, добавь первую букву "
                    f"фамилии: {examples}.")
        parts.append(ask)
        return "\n\n".join(parts)

    def send_welcome(self, chat_id) -> None:
        self.api.send_message(chat_id, self.welcome_text(chat_id))

    def handle_name(self, chat_id, text: str) -> None:
        """Любой текст без «/» — попытка написать своё имя."""
        current = self.store.subscribers.get(str(chat_id))
        name, candidates = self.book.resolve_typed(text, self.schedule)
        if name is not None:
            if name == current:
                self.api.send_message(chat_id, f"Ты уже записан(а) как «{name}».")
                return
            self.store.subscribers[str(chat_id)] = name
            self.store.save()
            verb = "Имя изменено" if current else "Готово"
            self.api.send_message(chat_id, f"{verb}: ты записан(а) как «{name}». "
                                  f"Каждый вечер после 21:00 пришлю репетиции на завтра."
                                  f"\n\n{HELP_TEXT}")
        elif candidates:
            self.api.send_message(chat_id, "Это имя есть у нескольких человек: "
                                  f"{', '.join(candidates)}. Напиши своё имя с первой "
                                  f"буквой фамилии, например «{candidates[0]}».")
        elif current:
            self.api.send_message(chat_id, f"Не понял. Ты записан(а) как «{current}»; "
                                  f"чтобы сменить имя, напиши его так, как в расписании."
                                  f"\n\n{HELP_TEXT}")
        else:
            self.api.send_message(chat_id, "Не нашёл такого имени среди участников. "
                                  "Напиши имя так, как оно записано в расписании, "
                                  "например: Соня или Миша К.")

    # -- общие команды (регистрация не нужна) ------------------------- #

    def _message_time(self, msg: dict) -> datetime | None:
        ts = msg.get("date")
        if not isinstance(ts, (int, float)):
            return None
        try:
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(ts, ZoneInfo(TZ_NAME))
        except Exception:
            return datetime.fromtimestamp(ts, timezone(timedelta(hours=3)))

    def _expired(self, since: str) -> bool:
        try:
            started = datetime.fromisoformat(since)
        except (TypeError, ValueError):
            return True
        now = self.now()
        if (started.tzinfo is None) != (now.tzinfo is None):
            started, now = started.replace(tzinfo=None), now.replace(tzinfo=None)
        return now - started > AWAIT_WHO_TTL

    def _pop_awaiting_who(self, chat_id) -> bool:
        """Ждали ли от этого чата имя после /who. Ожидание снимается в любом случае,
        просроченные записи чистятся; пустой словарь в state.json не хранится."""
        waiting: dict = self.store.state.get("awaiting_who") or {}
        since = waiting.pop(str(chat_id), None)
        self.cleanup_awaiting()
        return since is not None and not self._expired(since)

    def cleanup_awaiting(self) -> None:
        """Удаляет просроченные ожидания /who; пустой словарь в state.json не хранится."""
        waiting: dict = self.store.state.get("awaiting_who") or {}
        for chat, started in list(waiting.items()):
            if self._expired(started):
                waiting.pop(chat)
        if waiting:
            self.store.state["awaiting_who"] = waiting
        else:
            self.store.state.pop("awaiting_who", None)

    def send_at(self, chat_id, day: date, at: time) -> None:
        items = running_at(self.schedule, day, at)
        if not items:
            self.api.send_message(chat_id, NOTHING_AT_TIME)
            return
        self.api.send_message(chat_id, format_block(
            f"РЕПЕТИЦИИ {day:%d.%m.%y} В {at:%H:%M}:", items))

    def handle_at(self, chat_id, args: list[str]) -> None:
        today = self.request_now().date()
        # «17.11» похоже и на время 17:11, и на дату: время с «:» — всегда время;
        # без «:» первое — время, второе — дата
        time_tok = next((a for a in args if ":" in a), args[0] if args else "")
        rest = [a for a in args if a is not time_tok]
        at = parse_hhmm(time_tok)
        day = parse_ddmm(rest[0], today) if rest else today
        if at is None or day is None or len(args) > 2:
            self.api.send_message(chat_id, "Напиши время так: /at 16:30 или /at 16:30 17.11")
            return
        self.send_at(chat_id, day, at)

    def ask_who(self, chat_id) -> None:
        names = self.book.registration_names(self.schedule)
        waiting = self.store.state.get("awaiting_who") or {}
        waiting[str(chat_id)] = self.now().isoformat(timespec="seconds")
        self.store.state["awaiting_who"] = waiting
        self.store.save()
        self.api.send_message(
            chat_id, "Чьё расписание показать? Нажми имя или напиши его следующим сообщением.",
            keyboard([{"text": n, "callback_data": "w:" + self.book.key(n)} for n in names]))

    def send_who(self, chat_id, text: str) -> None:
        name, candidates = self.book.resolve_typed(text, self.schedule)
        if name is not None:
            self.send_who_schedule(chat_id, name)
        elif candidates:
            self.api.send_message(
                chat_id, "Это имя есть у нескольких человек. Кого показать?",
                keyboard([{"text": n, "callback_data": "w:" + self.book.key(n)}
                          for n in candidates]))
        else:
            self.api.send_message(chat_id, "Не нашёл такого имени среди участников. "
                                  "Отправь /who без имени, чтобы увидеть список.")

    def send_who_schedule(self, chat_id, name: str) -> None:
        today = self.request_now().date()
        blocks = [format_block(f"РАСПИСАНИЕ {name} {d:%d.%m.%y}:",
                               rehearsals_for(name, d, self.schedule, self.book))
                  for d in (today, today + timedelta(days=1))]
        self.api.send_message(chat_id, "\n\n".join(blocks))

    def rooms(self) -> list[str]:
        rooms = {r.room for r in self.schedule if r.room}
        return sorted(rooms, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))

    def handle_room(self, chat_id, args: list[str]) -> None:
        today = self.request_now().date()
        day = next((d for d in (parse_ddmm(a, today) for a in args) if d), None)
        room = next((a for a in args if parse_ddmm(a, today) is None), None)
        if room is None:
            rooms = self.rooms()
            if not rooms:
                self.api.send_message(chat_id, "В расписании пока нет аудиторий.")
                return
            suffix = f":{day.isoformat()}" if day else ""
            self.api.send_message(chat_id, "Какая аудитория?", keyboard(
                [{"text": r, "callback_data": f"r:{r}{suffix}"} for r in rooms]))
            return
        self.send_room(chat_id, room, [day] if day else None)

    def send_room(self, chat_id, room: str, days: list[date] | None) -> None:
        room = room.strip()
        if room not in self.rooms():
            self.api.send_message(chat_id, f"Аудитории «{room}» нет в расписании. "
                                  f"Есть: {', '.join(self.rooms()) or '—'}.")
            return
        if days is None:
            today = self.request_now().date()
            days = [today, today + timedelta(days=1)]
        blocks = [format_block(f"АУДИТОРИЯ {room} {d:%d.%m.%y}:",
                               [r for r in self.schedule if r.date == d and r.room == room])
                  for d in days]
        self.api.send_message(chat_id, "\n\n".join(blocks))

    def send_find(self, chat_id, query: str) -> None:
        query = query.strip()
        if not query:
            self.api.send_message(chat_id, "Напиши часть названия: /find эрнест")
            return
        # каждое слово запроса — часть названия, порядок не важен:
        # «триумф муз» находит «Триумф любви муз (Овчинникова)»
        words = normalize(query).split()
        now = self.request_now()
        found = [r for r in self.schedule
                 if all(w in normalize(r.title) for w in words) and not ended(r, now)]
        found.sort(key=lambda r: (r.date, *_by_time(r)))
        if not found:
            self.api.send_message(chat_id, f"Ничего не нашёл по «{query}» на ближайшие дни.")
            return
        shown = found[:FIND_LIMIT]
        blocks = [format_block(f"{d:%d.%m.%y}:", [r for r in shown if r.date == d])
                  for d in sorted({r.date for r in shown})]
        more = (f"\n\nи ещё {len(found) - len(shown)} — уточни название"
                if len(found) > len(shown) else "")
        self.api.send_message(chat_id, f"НАЙДЕНО «{query}»:\n\n" + "\n\n".join(blocks) + more)

    def handle_free(self, chat_id, args: list[str]) -> None:
        today = self.request_now().date()
        day = next((d for d in (parse_ddmm(a, today) for a in args) if d), None)
        rest = [a for a in args if parse_ddmm(a, today) is None]
        if len(rest) > 1:
            self.api.send_message(chat_id, "Напиши так: /free, /free 301 или /free 301 18.11")
            return
        rooms = self.rooms()
        if rest and rest[0] not in rooms:
            self.api.send_message(chat_id, f"Аудитории «{rest[0]}» нет в расписании. "
                                  f"Есть: {', '.join(rooms) or '—'}.")
            return
        days = [day] if day else [today, today + timedelta(days=1)]
        blocks = [format_free(room, d, self.schedule)
                  for room in (rest or rooms) for d in days]
        self.api.send_message(chat_id, "\n\n".join(blocks) or "В расписании пока нет аудиторий.")

    # -- личные команды ------------------------------------------------- #

    def _need_name(self, chat_id) -> str | None:
        name = self.store.subscribers.get(str(chat_id))
        if not name:
            self.api.send_message(chat_id, "Сначала напиши мне своё имя (подробнее — /start).")
        return name

    def handle_date(self, chat_id, args: list[str]) -> None:
        name = self._need_name(chat_id)
        if not name:
            return
        day = parse_ddmm(args[0], self.request_now().date()) if len(args) == 1 else None
        if day is None:
            self.api.send_message(chat_id, "Напиши дату так: /date 18.11")
            return
        items = rehearsals_for(name, day, self.schedule, self.book)
        if not items:
            self.api.send_message(chat_id, f"{name}, на {day:%d.%m.%y} репетиций нет.")
            return
        self._deliver_day(chat_id, name, day, format_day(day, items))

    def send_next(self, chat_id) -> None:
        name = self._need_name(chat_id)
        if not name:
            return
        now = self.request_now()
        mine = [r for r in self.schedule
                if r.date is not None and self.book.matches(name, r) and not ended(r, now)]
        if not mine:
            self.api.send_message(chat_id, f"{name}, ближайших репетиций нет.")
            return
        first = min(mine, key=lambda r: (r.date, *_by_time(r)))
        # параллельные — все, что начинаются тогда же
        same = [r for r in mine if r.date == first.date and r.start == first.start]
        if any(is_running(r, now) for r in same):
            title = "СЕЙЧАС ИДЁТ:"
        elif first.date == now.date():
            title = "БЛИЖАЙШАЯ РЕПЕТИЦИЯ СЕГОДНЯ:"
        else:
            title = f"БЛИЖАЙШАЯ РЕПЕТИЦИЯ {first.date:%d.%m.%y}:"
        self.api.send_message(chat_id, format_block(title, same))

    def set_muted(self, chat_id, muted: bool) -> None:
        if not self._need_name(chat_id):
            return
        chat = str(chat_id)
        if muted == self.store.is_muted(chat):
            self.api.send_message(chat_id, "Рассылка и уведомления уже выключены. Включить — /unmute."
                                  if muted else "Рассылка и уведомления уже включены.")
            return
        (self.store.muted.add if muted else self.store.muted.discard)(chat)
        self.store.save()
        self.api.send_message(
            chat_id,
            "Вечерняя рассылка и уведомления об изменениях выключены. Команды /today, "
            "/next и другие работают как обычно. Включить снова — /unmute."
            if muted else "Вечерняя рассылка и уведомления об изменениях снова включены.")


    def send_personal(self, chat_id, command: str) -> None:
        name = self.store.subscribers.get(str(chat_id))
        if not name:
            self.api.send_message(chat_id, "Сначала зарегистрируйся: /start")
            return
        today = self.request_now().date()
        days = {"/today": [today], "/tomorrow": [today + timedelta(days=1)],
                "/week": [today + timedelta(days=i) for i in range(7)]}[command]
        blocks = []
        for d in days:
            items = rehearsals_for(name, d, self.schedule, self.book)
            if items:
                blocks.append(format_day(d, items))
        if not blocks:
            period = {"/today": "сегодня", "/tomorrow": "завтра",
                      "/week": "в ближайшие 7 дней"}[command]
            self.api.send_message(chat_id, f"{name}, {period} репетиций нет.")
            return
        if command == "/week":                          # картинка — только для одного дня
            self.api.send_message(chat_id, "\n\n".join(blocks))
        else:
            self._deliver_day(chat_id, name, days[0], blocks[0])

    # -- рассылка -------------------------------------------------------- #

    def send_daily(self, force: bool = False) -> int:
        """Рассылка на завтра: в первый запуск с daily_hour (21:00) и до полуночи.

        Раньше 21:00 ничего не отправляется (запуск просто пропускается);
        повторные запуски в тот же вечер отсекаются по last_daily.
        """
        now = self.now()
        if not force and now.hour < self.daily_hour:
            log(f"Сейчас {now:%H:%M}, рассылка только с {self.daily_hour:02d}:00 — пропускаю")
            return 0
        day = now.date() + timedelta(days=1)
        if not force and self.store.state.get("last_daily") == day.isoformat():
            log(f"Рассылка на {day:%d.%m.%y} уже была, пропускаю")
            return 0
        sent = failed = 0
        for chat_id, name in list(self.store.subscribers.items()):
            if self.store.is_muted(chat_id):
                continue                                 # /mute
            items = rehearsals_for(name, day, self.schedule, self.book)
            if not items:
                continue
            if self._deliver_day(chat_id, name, day, format_day(day, items)):
                sent += 1
            else:
                failed += 1
        self.store.state["last_daily"] = day.isoformat()
        self.store.save()
        log(f"Рассылка на {day:%d.%m.%y}: отправлено {sent}, ошибок {failed}")
        return sent


    def _render_day(self, name: str, day: date) -> bytes | None:
        try:
            from render_image import render_schedule_image
            return render_schedule_image(day, name, self.schedule, self.now(), book=self.book)
        except Exception as e:  # noqa: BLE001 — картинка не должна мешать тексту
            log(f"Картинка не нарисована: {type(e).__name__}: {e}")
            return None

    def _deliver_day(self, chat_id, name: str, day: date, text: str) -> bool:
        """Расписание дня: картинка-сетка и текст.

        * текст до CAPTION_LIMIT — одно сообщение: sendPhoto с подписью;
        * длиннее — картинка без подписи, затем полный текст отдельным сообщением;
        * sendPhoto не удался (или картинку не нарисовать) — текст отдельным сообщением.
        Текст никогда не обрезается. True — если текст дошёл.
        """
        chat_id = str(chat_id)
        send_photo = getattr(self.api, "send_photo", None)
        png = self._render_day(name, day) if send_photo else None
        if png is not None:
            caption = text if tg_len(text) <= CAPTION_LIMIT else None
            try:
                send_photo(int(chat_id), png, filename=f"schedule-{day:%Y-%m-%d}.png",
                           caption=caption)
                if caption is not None:
                    return True                      # всё в одном сообщении
            except TelegramError as e:
                if e.code == 403:                    # бот заблокирован — текст тоже не дойдёт
                    self.store.subscribers.pop(chat_id, None)
                    log(f"Не удалось отправить сообщение: {e}")
                    return False
                log(f"Картинка не отправлена: {e}")
            except Exception as e:  # noqa: BLE001
                log(f"Картинка не отправлена: {type(e).__name__}: {e}")
        return self._deliver(chat_id, text)

    def _deliver(self, chat_id: str, text: str) -> bool:
        try:
            for part in split_message(text):
                self.api.send_message(int(chat_id), part)
            return True
        except TelegramError as e:
            if e.code == 403:  # пользователь заблокировал бота — удаляем данные
                self.store.subscribers.pop(chat_id, None)
            log(f"Не удалось отправить сообщение: {e}")
            return False

    # -- изменения в расписании ----------------------------------------- #

    def check_changes(self) -> int:
        """Сравнивает таблицу со снимком и уведомляет затронутых подписчиков.

        Таблица не скачалась или пустая — ничего не делаем и снимок не трогаем.
        Снимка нет (первый запуск) — только создаём его.
        """
        try:
            current = self.schedule
        except Exception as e:
            log(f"Таблица не скачалась ({type(e).__name__}), изменения не проверяю")
            return 0
        if not current:
            log("Таблица пришла пустой, изменения не проверяю")
            return 0

        today = self.today()
        new = ch.upcoming(current, today)
        old = ch.load_snapshot(self.snapshot_file)
        if old is None:
            ch.save_snapshot(new, self.snapshot_file)
            log(f"Создан снимок расписания ({len(new)} записей) — первый запуск "
                f"или сменилась вкладка таблицы; уведомлений нет")
            return 0

        changes = ch.diff(ch.upcoming(old, today), new, self.book)
        sent = 0
        if changes:
            for chat_id, name in list(self.store.subscribers.items()):
                if self.store.is_muted(chat_id):
                    continue                             # /mute
                mine = [c for c in changes if ch.affected(c, name, self.book)]
                if mine and self._deliver(chat_id, format_changes(mine)):
                    sent += 1
            self.store.save()
        ch.save_snapshot(new, self.snapshot_file)
        log(f"Изменений в расписании: {len(changes)}, уведомлено подписчиков: {sent}")
        return sent


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def check_names() -> int:
    """Печатает всё, что требует ручной проверки. Код выхода 1, если есть что проверять."""
    book = NameBook.load()
    schedule = load_schedule()
    print("Имена для регистрации:", ", ".join(book.registration_names(schedule)))
    problems = 0

    def where(r: Rehearsal) -> str:
        return f"{r.date:%d.%m.%y} {format_time(r)} {r.title}, ауд. {r.room}"

    mentions = book.ambiguous(schedule)
    if mentions:
        problems += 1
        print("\nНЕОДНОЗНАЧНЫЕ — имя без буквы фамилии (рассылки по нему нет):")
        for m in mentions:
            print(f"  «{m.name}»: {where(m.rehearsal)} — возможно: {' / '.join(m.candidates)}")

    unknown = book.unknown(schedule)
    if unknown:
        problems += 1
        print("\nНЕПОНЯТНЫЕ — не человек из people.json и не группа (рассылки по ним нет).\n"
              "Если это имя — добавь в people.json, если вариант имени — в aliases.json:")
        for m in unknown:
            print(f"  «{m.name}»: {where(m.rehearsal)}")

    unsure = book.unknown_gender()
    if unsure:
        problems += 1
        print("\nПОЛ «?» в people.json — рассылки «девушкам»/«парням» им нет:")
        print("  " + ", ".join(unsure))

    if not problems:
        print("\nВсё в порядке, проверять нечего.")
    return 1 if problems else 0


class DryRunAPI:
    """Подмена Telegram для локальной проверки: ничего не отправляет, печатает."""

    def __init__(self, updates: list[dict] | None = None):
        self._updates = list(updates or [])

    def call(self, method: str, **params):
        if method == "getUpdates":
            res = [u for u in self._updates if u["update_id"] >= params.get("offset", 0)]
            self._updates = []
            return res
        return True

    def send_photo(self, chat_id, png: bytes, filename: str = "schedule.png",
                   caption: str | None = None):
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / f"dry-run-{chat_id}-{filename}"
        path.write_bytes(png)
        print(f"\n----- [dry-run] картинка в chat {chat_id}: {path} -----")
        if caption:
            print(f"[подпись]\n{caption}")
        return {}

    def send_message(self, chat_id, text: str, keyboard: list | None = None):
        print(f"\n----- [dry-run] сообщение в chat {chat_id} -----\n{text}")
        for row in keyboard or []:
            print("  " + "  ".join(f"[{b['text']}]" for b in row))
        return {}


def _dry_run_store(reset_offset: bool = False) -> tuple[Store, Path]:
    """Копии state/subscribers/snapshot во временной папке — репозиторий не меняется.

    reset_offset: обнулить offset в копии. Нужно для --updates: в настоящем
    state.json offset реального бота (например 225635883), и тестовые сообщения
    с номерами 1, 2, 3… иначе считались бы уже обработанными.
    """
    tmp = Path(tempfile.mkdtemp(prefix="bot-dry-run-"))
    for f in (STATE_FILE, SUBSCRIBERS_FILE, ch.SNAPSHOT_FILE):
        if f.exists():
            shutil.copy(f, tmp / f.name)
    store = Store(tmp / STATE_FILE.name, tmp / SUBSCRIBERS_FILE.name)
    if reset_offset:
        store.state["offset"] = 0
    return store, tmp / ch.SNAPSHOT_FILE.name


def preview(name: str, day_str: str | None) -> int:
    book = NameBook.load()
    day = (datetime.strptime(day_str, "%d.%m.%y").date() if day_str
           else local_now().date() + timedelta(days=1))
    items = rehearsals_for(name, day, load_schedule(), book)
    if not book.is_known(name):
        print(f"Внимание: «{name}» нет в people.json — такой подписчик не получит "
              f"репетиций по имени, только групповые «всем».")
    print(format_day(day, items) if items
          else f"{name}: на {day:%d.%m.%y} репетиций нет — сообщение не отправляется.")
    return 0


# Профиль бота в Telegram (python bot.py setup_profile, workflow setup_profile.yml).
# Лимиты Bot API: имя до 64, короткое описание до 120, описание до 512 символов.
PROFILE_NAME = "Расписание репетиций"
PROFILE_SHORT_DESCRIPTION = ("Присылает ваше расписание репетиций каждый вечер и сообщает, "
                             "если в таблице что-то поменялось.")
PROFILE_DESCRIPTION = """Расписание репетиций вашего курса.

Что умею:
• каждый вечер в 21:00 присылаю ваше расписание на завтра: картинка-сетка и текст
• сразу сообщаю, если в таблице что-то поменялось
• отвечаю на команды: что идёт сейчас, расписание друга, репетиции в аудитории

Нажмите «Начать» и выберите своё имя."""
PROFILE_LIMITS = {"name": 64, "short_description": 120, "description": 512}


def profile_calls() -> list[tuple[str, dict]]:
    """Вызовы Bot API, которые ставят профиль. Команды — из того же COMMANDS, что /help."""
    return [
        ("setMyName", {"name": PROFILE_NAME}),
        ("setMyShortDescription", {"short_description": PROFILE_SHORT_DESCRIPTION}),
        ("setMyDescription", {"description": PROFILE_DESCRIPTION}),
        ("setMyCommands", {"commands": TELEGRAM_COMMANDS}),
    ]


def setup_profile(apply: bool, api=None) -> int:
    """Без apply — только печатает, что будет установлено. С apply — вызывает Bot API.
    Токен — только из TELEGRAM_BOT_TOKEN; ошибки Telegram печатаются без него."""
    fields = {"name": PROFILE_NAME, "short_description": PROFILE_SHORT_DESCRIPTION,
              "description": PROFILE_DESCRIPTION}
    too_long = False
    for field, value in fields.items():
        n, limit = tg_len(value), PROFILE_LIMITS[field]
        too_long |= n > limit
        print(f"----- {field}: {n} из {limit}{' — ДЛИННЕЕ ЛИМИТА' if n > limit else ''}")
        print(value)
    print(f"----- commands: {len(TELEGRAM_COMMANDS)}")
    for c in TELEGRAM_COMMANDS:
        print(f"/{c['command']} — {c['description']}")
    if too_long:
        print("Ошибка: текст длиннее лимита Telegram — ничего не устанавливаю")
        return 2
    if not apply:
        print("Режим проверки: ничего не установлено (для установки — apply=true)")
        return 0
    api = api or TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    failed = 0
    for method, params in profile_calls():
        try:
            api.call(method, **params)
            print(f"{method}: готово")
        except TelegramError as e:      # например, лимит частоты для setMyName
            failed += 1
            print(f"{method}: не удалось — {e}")
    return 1 if failed else 0


BROADCAST_FILE = ROOT / "broadcast_message.txt"
BROADCAST_CAPTION_MAX = 1024       # лимит Telegram для подписи к фото
BROADCAST_PAUSE = 0.1              # секунд между отправками


def broadcast(send: bool, force: bool, api=None, store: Store | None = None,
              text_file: Path = BROADCAST_FILE, banner: bytes | None = None,
              sleep: Callable[[float], None] = None) -> int:
    """Разовое объявление всем подписчикам (и тем, у кого /mute):
    одно сообщение — sendPhoto с баннером и текстом в подписи.

    send=False — проверка: текст, длина, число получателей; ничего не отправляется.
    Повтор того же текста блокируется хэшем в state.json (broadcast_hashes),
    force=True разрешает отправить ещё раз. В лог — только счётчики.
    """
    import time as time_module
    sleep = sleep or time_module.sleep
    store = store or Store()
    text = text_file.read_text(encoding="utf-8").strip()
    length = tg_len(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    already = digest in store.state.get("broadcast_hashes", [])
    recipients = list(store.subscribers)

    print("----- текст объявления -----")
    print(text)
    print("----------------------------")
    print(f"Длина: {length} из {BROADCAST_CAPTION_MAX} (как считает Telegram)")
    print(f"Получателей: {len(recipients)} (включая тех, у кого /mute)")
    if already:
        print("Этот текст уже рассылался" + (" — force: отправлю ещё раз" if force else ""))
    if not text or length > BROADCAST_CAPTION_MAX:
        print("Ошибка: текст пустой или длиннее лимита подписи — не отправляю")
        return 2
    if not send:
        print("Режим проверки: ничего не отправлено (для отправки — send=true)")
        return 0
    if already and not force:
        print("Повтор не отправляю. Чтобы отправить ещё раз, включи force.")
        return 0

    if banner is None:
        from render_banner import BANNER_FILE, render_banner
        banner = BANNER_FILE.read_bytes() if BANNER_FILE.exists() else render_banner()
    api = api or TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    sent = failed = 0
    try:
        for i, chat in enumerate(recipients):
            if i:
                sleep(BROADCAST_PAUSE)
            try:
                api.send_photo(int(chat), banner, filename="update.png", caption=text)
                sent += 1
            except TelegramError as e:
                failed += 1
                if e.code == 403:                # заблокировал бота — удаляем, как в рассылке
                    store.subscribers.pop(chat, None)
    finally:
        if sent:
            hashes = store.state.get("broadcast_hashes", [])
            if digest not in hashes:
                hashes.append(digest)
            store.state["broadcast_hashes"] = hashes
        store.save()
        log(f"Объявление: отправлено {sent}, не удалось {failed}")
    return 0


def save_image(args: list[str]) -> int:
    """python bot.py image ИМЯ [дд.мм.гг] -> out/preview.png (без Telegram)."""
    from render_image import render_schedule_image
    book = NameBook.load()
    schedule = load_schedule()
    day_arg = next((a for a in args if _DATE_ARG.fullmatch(a)), None)
    typed = " ".join(a for a in args if a != day_arg)
    name, candidates = book.resolve_typed(typed, schedule)
    if name is None:
        print(f"Имя «{typed}» неоднозначно: {', '.join(candidates)}" if candidates
              else f"Не нашёл «{typed}» в people.json / aliases.json")
        return 2
    now = local_now()
    day = (parse_ddmm(day_arg, now.date()) if day_arg else now.date() + timedelta(days=1))
    if day is None:
        print(f"Не понял дату «{day_arg}», нужно дд.мм.гг")
        return 2
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "preview.png"
    path.write_bytes(render_schedule_image(day, name, schedule, now, book=book))
    mine = len(rehearsals_for(name, day, schedule, book))
    print(f"{path} — {name}, {day:%d.%m.%y}: репетиций у человека {mine}, "
          f"всего в этот день {sum(1 for r in schedule if r.date == day)}"
          + ("" if mine else " (в рассылке такая картинка не отправится)"))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bot.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="run",
                        choices=["poll", "send_daily", "run", "check_names", "preview", "image",
                                 "broadcast", "setup_profile"])
    parser.add_argument("--apply", action="store_true", help="setup_profile: вызвать Bot API")
    parser.add_argument("--send", action="store_true", help="broadcast: отправить (иначе проверка)")
    parser.add_argument("args", nargs="*", help="для preview и image: ИМЯ [дд.мм.гг]")
    parser.add_argument("--force", action="store_true",
                        help="send_daily: не ждать 21:00; broadcast: отправить повторно")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--updates", type=Path, help="dry-run: входящие сообщения из JSON-файла")
    parser.add_argument("--now", help='dry-run: подменить время, «ГГГГ-ММ-ДД ЧЧ:ММ» (МСК)')
    opts = parser.parse_args(argv[1:])

    if opts.mode == "check_names":
        return check_names()
    if opts.mode == "setup_profile":
        return setup_profile(apply=opts.apply)
    if opts.mode == "broadcast":
        return broadcast(send=opts.send, force=opts.force)
    if opts.mode == "image":
        if not opts.args:
            parser.error("image: укажи имя, например: image Соня 12.11.26")
        return save_image(opts.args)
    if opts.mode == "preview":
        if not opts.args:
            parser.error("preview: укажи имя, например: preview Соня 12.11.26")
        return preview(" ".join(a for a in opts.args if not _DATE_ARG.fullmatch(a)),
                       next((a for a in opts.args if _DATE_ARG.fullmatch(a)), None))
    if (opts.updates or opts.now) and not opts.dry_run:
        parser.error("--updates и --now работают только вместе с --dry-run")

    if opts.dry_run:
        updates = read_json(opts.updates, []) if opts.updates else []
        store, snapshot = _dry_run_store(reset_offset=bool(opts.updates))
        now = local_now
        if opts.now:
            fixed = datetime.strptime(opts.now, "%Y-%m-%d %H:%M")
            now = lambda: fixed  # noqa: E731
        bot = Bot(DryRunAPI(updates), store, NameBook.load(), now=now, snapshot_file=snapshot)
        log(f"[dry-run] сообщения не отправляются, файлы — во временной папке {snapshot.parent}")
    else:
        bot = Bot(TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", "")), Store(), NameBook.load())

    if opts.mode in ("poll", "run"):
        log(f"Обработано обновлений: {bot.poll()}")
        bot.check_changes()
    if opts.mode == "send_daily":
        bot.send_daily(force=opts.force)
    elif opts.mode == "run":
        bot.send_daily()  # сам решит, пора ли (с 21:00 и ещё не отправлено)
    if opts.dry_run:
        log(f"[dry-run] подписчики после запуска: {store.subscribers}")
    return 0


_DATE_ARG = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2}")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except TelegramError as e:
        log(f"Ошибка Telegram: {e}")
        sys.exit(1)
