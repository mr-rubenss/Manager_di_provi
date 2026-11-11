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
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

import changes as ch
from names import NameBook, group_of
from schedule_parser import Participant, Rehearsal, load_schedule
from storage import read_json, write_json

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
SUBSCRIBERS_FILE = ROOT / "subscribers.json"

TZ_NAME = os.environ.get("BOT_TIMEZONE", "Europe/Moscow")
DAILY_HOUR = int(os.environ.get("DAILY_HOUR", "21"))  # рассылка — с 21:00 по BOT_TIMEZONE

HELP_TEXT = (
    "Команды:\n"
    "/today — мои репетиции сегодня\n"
    "/tomorrow — мои репетиции завтра\n"
    "/week — мои репетиции на 7 дней\n"
    "/stop — отписаться и удалить мои данные\n"
    "/start — что умеет бот и как сменить имя"
)
ABOUT_TEXT = (
    "Привет! Я присылаю расписание репетиций из общей таблицы.\n\n"
    "Что я умею:\n"
    "• каждый вечер после 21:00 присылаю твои репетиции на завтра;\n"
    "• сообщаю, если в расписании что-то поменялось;\n"
    "• показываю твои репетиции по командам.\n\n"
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
    if group_of(p) is not None:
        return f"{p.name} ({p.note})"  # группа — как в таблице: «хор (женщины)»
    return p.name + "?" if p.note == "?" else f"{p.name} [{p.note}]"


def format_line(r: Rehearsal) -> str:
    people = ", ".join(format_participant(p) for p in r.participants)
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

    def today(self) -> date:
        return self.now().date()

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

        if not text.startswith("/"):
            self.handle_name(chat_id, text)
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
            items = rehearsals_for(name, day, self.schedule, self.book)
            if not items:
                continue
            if self._deliver(chat_id, format_day(day, items)):
                sent += 1
            else:
                failed += 1
        self.store.state["last_daily"] = day.isoformat()
        self.store.save()
        log(f"Рассылка на {day:%d.%m.%y}: отправлено {sent}, ошибок {failed}")
        return sent


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bot.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="run",
                        choices=["poll", "send_daily", "run", "check_names", "preview"])
    parser.add_argument("args", nargs="*", help="для preview: ИМЯ [дд.мм.гг]")
    parser.add_argument("--force", action="store_true", help="send_daily: не ждать 21:00")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--updates", type=Path, help="dry-run: входящие сообщения из JSON-файла")
    parser.add_argument("--now", help='dry-run: подменить время, «ГГГГ-ММ-ДД ЧЧ:ММ» (МСК)')
    opts = parser.parse_args(argv[1:])

    if opts.mode == "check_names":
        return check_names()
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
