"""Telegram-бот расписания репетиций. Работает «по вызову» (GitHub Actions).

Режимы:
  python bot.py poll                 — обработать новые сообщения (getUpdates)
  python bot.py send_daily [--force] — разослать подписчикам репетиции на завтра
  python bot.py run                  — poll + send_daily, если наступил час рассылки
                                       и она ещё не сделана (запасной путь для poll.yml)
  python bot.py check_names          — показать имена, которые нужно проверить

Токен берётся только из переменной окружения TELEGRAM_BOT_TOKEN и нигде
не печатается: ошибки сети очищаются от URL с токеном.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from names import NameBook
from schedule_parser import Participant, Rehearsal, load_schedule

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
SUBSCRIBERS_FILE = ROOT / "subscribers.json"

TZ_NAME = os.environ.get("BOT_TIMEZONE", "Europe/Moscow")
DAILY_HOUR = int(os.environ.get("DAILY_HOUR", "21"))  # с этого часа run делает рассылку
NAME_BUTTONS_PER_ROW = 3

CONSENT_TEXT = (
    "Бот сохраняет твоё имя из расписания и Telegram chat_id в публичном "
    "репозитории GitHub. Продолжая, ты соглашаешься с этим. Отменить можно "
    "командой /stop — данные будут удалены."
)
HELP_TEXT = (
    "Команды:\n"
    "/today — мои репетиции сегодня\n"
    "/tomorrow — мои репетиции завтра\n"
    "/week — мои репетиции на 7 дней\n"
    "/stop — отписаться и удалить мои данные\n"
    "/start — зарегистрироваться или сменить имя"
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
        error = None
        try:
            resp = self._session.post(self._base + method, json=params,
                                      timeout=self._timeout)
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

def read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


class Store:
    def __init__(self, state_file: Path = STATE_FILE,
                 subscribers_file: Path = SUBSCRIBERS_FILE):
        self.state_file = state_file
        self.subscribers_file = subscribers_file
        self.state = {"offset": 0, "last_daily": None} | read_json(state_file, {})
        self.subscribers: dict[str, str] = read_json(subscribers_file, {})

    def save(self) -> None:
        write_json(self.state_file, self.state)
        write_json(self.subscribers_file, self.subscribers)


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
    return p.name + "?" if p.note == "?" else f"{p.name} [{p.note}]"


def format_day(day: date, rehearsals: list[Rehearsal]) -> str:
    # sorted() стабилен: параллельные репетиции в одно время остаются
    # отдельными строками в порядке таблицы
    items = sorted(rehearsals, key=lambda r: (r.start is None, r.start or datetime.min.time()))
    lines = [f"РАСПИСАНИЕ РЕПЕТИЦИЙ {day:%d.%m.%y}:"]
    for r in items:
        people = ", ".join(format_participant(p) for p in r.participants)
        lines.append(f"{format_time(r)} {r.title} "
                     f"(задействованы: {people}; аудитория {r.room or 'не указана'})")
    return "\n".join(lines)


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


def local_today() -> date:
    return local_now().date()


class Bot:
    def __init__(self, api: TelegramAPI, store: Store, book: NameBook,
                 schedule_loader: Callable[[], list[Rehearsal]] = load_schedule,
                 today: Callable[[], date] = local_today):
        self.api = api
        self.store = store
        self.book = book
        self._loader = schedule_loader
        self._schedule: list[Rehearsal] | None = None
        self.today = today

    @property
    def schedule(self) -> list[Rehearsal]:
        if self._schedule is None:
            self._schedule = self._loader()
        return self._schedule

    # -- poll ------------------------------------------------------------ #

    def poll(self) -> int:
        handled = 0
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
        command = text.split()[0].split("@")[0].lower()

        if command == "/start":
            self.api.send_message(chat_id, CONSENT_TEXT,
                                  [[{"text": "Согласен", "callback_data": "agree"}]])
        elif command == "/stop":
            if self.store.subscribers.pop(str(chat_id), None) is not None:
                self.store.save()
                self.api.send_message(chat_id, "Твои данные удалены. "
                                      "Чтобы подписаться снова, отправь /start.")
            else:
                self.api.send_message(chat_id, "Тебя нет среди подписчиков, удалять нечего.")
        elif command in ("/today", "/tomorrow", "/week"):
            self.send_personal(chat_id, command)
        else:
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

        if data == "agree":
            names = self.book.registration_names(self.schedule)
            if not names:
                self.api.send_message(chat_id, "В расписании пока нет имён. Попробуй позже.")
                return
            buttons = [{"text": n, "callback_data": "n:" + self.book.key(n)} for n in names]
            buttons = [b for b in buttons if len(b["callback_data"].encode()) <= 64]
            rows = [buttons[i:i + NAME_BUTTONS_PER_ROW]
                    for i in range(0, len(buttons), NAME_BUTTONS_PER_ROW)]
            self.api.send_message(chat_id, "Выбери своё имя из расписания:", rows)
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

    def send_personal(self, chat_id, command: str) -> None:
        name = self.store.subscribers.get(str(chat_id))
        if not name:
            self.api.send_message(chat_id, "Сначала зарегистрируйся: /start")
            return
        today = self.today()
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
        self.api.send_message(chat_id, "\n\n".join(blocks))

    # -- рассылка -------------------------------------------------------- #

    def send_daily(self, force: bool = False) -> int:
        day = self.today() + timedelta(days=1)
        if not force and self.store.state.get("last_daily") == day.isoformat():
            log(f"Рассылка на {day:%d.%m.%y} уже была, пропускаю")
            return 0
        sent = failed = 0
        for chat_id, name in list(self.store.subscribers.items()):
            items = rehearsals_for(name, day, self.schedule, self.book)
            if not items:
                continue
            try:
                self.api.send_message(int(chat_id), format_day(day, items))
                sent += 1
            except TelegramError as e:
                failed += 1
                if e.code == 403:  # пользователь заблокировал бота — удаляем данные
                    self.store.subscribers.pop(chat_id, None)
                log(f"Не удалось отправить сообщение: {e}")
        self.store.state["last_daily"] = day.isoformat()
        self.store.save()
        log(f"Рассылка на {day:%d.%m.%y}: отправлено {sent}, ошибок {failed}")
        return sent


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def check_names() -> int:
    book = NameBook.load()
    schedule = load_schedule()
    print("Имена для регистрации:", ", ".join(book.registration_names(schedule)))
    mentions = book.ambiguous(schedule)
    if not mentions:
        print("Неоднозначных имён нет.")
        return 0
    print("\nНУЖНО ПРОВЕРИТЬ — имя без буквы фамилии (рассылка по нему не идёт):")
    for m in mentions:
        r = m.rehearsal
        print(f"  «{m.name}»: {r.date:%d.%m.%y} {format_time(r)} {r.title}, ауд. {r.room}"
              f" — возможно: {' / '.join(m.candidates)}")
    return 1


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "run"
    if mode == "check_names":
        return check_names()
    if mode not in ("poll", "send_daily", "run"):
        print(__doc__)
        return 2
    bot = Bot(TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", "")), Store(), NameBook.load())
    if mode in ("poll", "run"):
        log(f"Обработано обновлений: {bot.poll()}")
    if mode == "send_daily":
        bot.send_daily(force="--force" in argv)
    elif mode == "run" and local_now().hour >= DAILY_HOUR:
        bot.send_daily()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except TelegramError as e:
        log(f"Ошибка Telegram: {e}")
        sys.exit(1)
