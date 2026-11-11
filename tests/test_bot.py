import json
import traceback
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

import requests

from bot import Bot, Store, TelegramAPI, TelegramError, format_day
from names import ALIASES_FILE, AliasError, NameBook
from schedule_parser import Rehearsal, parse_participants

D = date(2026, 11, 12)


def reh(title, people, start, end=None, room="301", day=D):
    return Rehearsal(date=day, weekday="четверг", start=start, end=end, time_raw="",
                     title=title, room=room, participants=parse_participants(people))


SCHEDULE = [
    reh("Альба", "Миша К, Соня", time(12), time(13), "309"),
    reh("110 в тени", "Миша П, Лиза", time(11), time(12)),
    reh("Хелоу Долли", "Софа, Вася О", time(11), time(12)),        # параллельно с «110 в тени»
    reh("Клоп", "Вася К, Федя, (Василиса?)", time(14), time(15)),
    reh("Войцек", "Миша, Тома, Василиса О", time(16), time(16, 30)),
    reh("Карусель", "весь курс, хор", time(17), time(18), day=date(2026, 11, 14)),  # всем
    reh("Завтрашнее", "Соня", time(10), time(11), day=date(2026, 11, 13)),
]


class FakeAPI:
    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []

    def call(self, method, **params):
        if method == "getUpdates":
            res = [u for u in self.updates if u["update_id"] >= params["offset"]]
            self.updates = []
            return res
        return True

    def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))


def make_bot(updates=None, now=datetime(2026, 11, 11, 21, 0)):
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "state.json", tmp / "subscribers.json")
    api = FakeAPI(updates)
    clock = {"now": now}
    bot = Bot(api, store, NameBook.load(), lambda: SCHEDULE, lambda: clock["now"],
              daily_hour=21, snapshot_file=tmp / "schedule_snapshot.json")
    bot.clock = clock  # тесты двигают время: bot.clock["now"] = ...
    return bot, api, store, tmp


def msg(uid, text, chat_id=42):
    return {"update_id": uid, "message": {"chat": {"id": chat_id, "type": "private"}, "text": text}}


def cb(uid, data, chat_id=42):
    return {"update_id": uid, "callback_query": {
        "id": str(uid), "data": data, "message": {"chat": {"id": chat_id, "type": "private"}}}}


class NamesTest(unittest.TestCase):
    def setUp(self):
        self.book = NameBook.load()

    def test_real_aliases(self):
        for a, b in [("Федя", "Фёдор"), ("Тома", "Тамара"), ("Лиза", "Соня"),
                     ("ФЕДОР", "фёдор"), ("Василиса К.", "василиса к"), ("вася  к", "Вася К")]:
            self.assertEqual(self.book.key(a), self.book.key(b), (a, b))

    def test_distinct_people_never_merge(self):
        for a, b in [("Миша К", "Миша П"), ("Вася О", "Вася К"), ("Софа", "Соня"),
                     ("Софа", "Лиза"), ("Миша", "Миша К"), ("Вася", "Вася О")]:
            self.assertNotEqual(self.book.key(a), self.book.key(b), (a, b))

    def test_distinct_people_matching(self):
        r = SCHEDULE[1]  # «Миша П, Лиза»
        self.assertTrue(self.book.matches("Миша П", r))
        self.assertTrue(self.book.matches("Соня", r))      # Лиза == Соня
        self.assertFalse(self.book.matches("Миша К", r))
        self.assertFalse(self.book.matches("Софа", r))
        self.assertFalse(self.book.matches("Миша", r))     # нет подстрочного поиска
        self.assertFalse(self.book.matches("Вася К", SCHEDULE[2]))  # там Вася О

    def test_bad_alias_rejected(self):
        for bad in ({"Миша К": "Миша П"}, {"Софа": "Соня"}, {"Лиза": "Софа"}):
            with self.assertRaises(AliasError):
                NameBook(bad)

    def test_registration_names(self):
        names = self.book.registration_names(SCHEDULE)
        self.assertIn("Соня", names)
        self.assertIn("Фёдор", names)
        self.assertIn("Тамара", names)
        for absent in ("Лиза", "Федя", "Тома", "Миша", "Василиса", "весь курс", "хор"):
            self.assertNotIn(absent, names)
        self.assertEqual(len(names), len(set(names)))

    def test_ambiguous_reported(self):
        found = {(m.name, m.candidates) for m in self.book.ambiguous(SCHEDULE)}
        self.assertEqual(found, {("Миша", ("Миша К", "Миша П")), ("Василиса", ("Василиса К.", "Василиса О"))})

    def test_aliases_file_is_valid_json(self):
        data = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["Лиза"], "Соня")


class FormatTest(unittest.TestCase):
    def test_daily_format_and_parallel(self):
        book = NameBook.load()
        items = [r for r in SCHEDULE if r.date == D and book.matches("Соня", r)]
        self.assertEqual(format_day(D, items),
                         "РАСПИСАНИЕ РЕПЕТИЦИЙ 12.11.26:\n"
                         "11:00-12:00 110 в тени (задействованы: Миша П, Лиза; аудитория 301)\n"
                         "12:00-13:00 Альба (задействованы: Миша К, Соня; аудитория 309)")

    def test_parallel_rehearsals_kept(self):
        text = format_day(D, SCHEDULE[1:3])
        self.assertIn("11:00-12:00 110 в тени", text)
        self.assertIn("11:00-12:00 Хелоу Долли", text)


class FlowTest(unittest.TestCase):
    def test_consent_then_register_then_stop(self):
        bot, api, store, tmp = make_bot([msg(1, "/start")])
        bot.poll()
        self.assertIn("публичном репозитории GitHub", api.sent[-1][1])
        self.assertEqual(api.sent[-1][2], [[{"text": "Согласен", "callback_data": "agree"}]])
        self.assertEqual(store.subscribers, {})                     # без согласия — ничего
        self.assertEqual(json.loads((tmp / "subscribers.json").read_text()), {})

        api.updates = [cb(2, "agree")]
        bot.poll()
        labels = [b["text"] for row in api.sent[-1][2] for b in row]
        self.assertIn("Соня", labels)
        self.assertNotIn("Лиза", labels)
        self.assertEqual(store.subscribers, {})

        api.updates = [cb(3, "n:" + bot.book.key("Соня"))]
        bot.poll()
        self.assertEqual(json.loads((tmp / "subscribers.json").read_text()), {"42": "Соня"})
        self.assertEqual(json.loads((tmp / "state.json").read_text())["offset"], 4)

        api.updates = [msg(4, "/stop")]
        bot.poll()
        self.assertEqual(json.loads((tmp / "subscribers.json").read_text()), {})

    def test_unknown_name_callback_not_saved(self):
        bot, api, store, _ = make_bot([cb(1, "n:миша")])
        bot.poll()
        self.assertEqual(store.subscribers, {})

    def test_send_daily(self):
        bot, api, store, _ = make_bot(now=datetime(2026, 11, 11, 21, 0))
        store.subscribers = {"1": "Соня", "2": "Софа", "3": "Арсений", "4": "Фёдор"}
        bot.send_daily()
        sent = {c: t for c, t, _ in api.sent}
        self.assertEqual(set(sent), {1, 2, 4})                     # у Арсения пусто — не шлём
        self.assertIn("110 в тени", sent[1])
        self.assertNotIn("Хелоу Долли", sent[1])                   # Софа != Соня
        self.assertIn("Хелоу Долли", sent[2])
        self.assertNotIn("110 в тени", sent[2])
        self.assertIn("Клоп (задействованы: Вася К, Федя, Василиса?; аудитория 301)", sent[4])
        bot.send_daily()                                           # повтор в тот же день
        self.assertEqual(len(api.sent), 3)

    def test_daily_only_from_21_msk(self):
        bot, api, store, _ = make_bot(now=datetime(2026, 11, 11, 18, 0))
        store.subscribers = {"1": "Соня"}
        for t in ((18, 0), (20, 59)):                  # до 21:00 — ничего
            bot.clock["now"] = datetime(2026, 11, 11, *t)
            self.assertEqual(bot.send_daily(), 0)
        self.assertEqual(api.sent, [])
        self.assertIsNone(store.state["last_daily"])

        bot.clock["now"] = datetime(2026, 11, 11, 21, 7)  # первый запуск после 21:00
        self.assertEqual(bot.send_daily(), 1)
        self.assertTrue(api.sent[0][1].startswith("РАСПИСАНИЕ РЕПЕТИЦИЙ 12.11.26:"))

        for t in ((21, 12), (23, 55)):                 # следующие запуски — не шлют
            bot.clock["now"] = datetime(2026, 11, 11, *t)
            self.assertEqual(bot.send_daily(), 0)
        self.assertEqual(len(api.sent), 1)

        bot.clock["now"] = datetime(2026, 11, 12, 0, 30)  # после полуночи — уже не «вечер»
        self.assertEqual(bot.send_daily(), 0)
        self.assertEqual(len(api.sent), 1)

    def test_daily_force_ignores_hour(self):
        bot, api, store, _ = make_bot(now=datetime(2026, 11, 11, 12, 0))
        store.subscribers = {"1": "Соня"}
        self.assertEqual(bot.send_daily(force=True), 1)

    def test_run_mode_polls_and_sends_after_21(self):
        bot, api, store, _ = make_bot([msg(1, "/stop", chat_id=7)],
                                      now=datetime(2026, 11, 11, 20, 55))
        store.subscribers = {"1": "Соня"}
        bot.poll(); bot.send_daily()                   # как `run` в 20:55
        self.assertEqual([c for c, *_ in api.sent], [7])  # только ответ на /stop
        bot.clock["now"] = datetime(2026, 11, 11, 21, 0)
        bot.poll(); bot.send_daily()                   # как `run` в 21:00
        self.assertEqual([c for c, *_ in api.sent], [7, 1])

    def test_today_command(self):
        bot, api, store, _ = make_bot([msg(1, "/today"), msg(2, "/week")],
                                      now=datetime(2026, 11, 12, 9, 0))
        store.subscribers = {"42": "Тамара"}
        bot.poll()
        self.assertIn("16:00-16:30 Войцек", api.sent[0][1])
        self.assertIn("Войцек", api.sent[1][1])


class TokenTest(unittest.TestCase):
    def test_token_not_in_errors(self):
        class Boom(requests.Session):
            def post(self, url, **kw):
                raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

        token = "123456:SECRET-token"
        api = TelegramAPI(token, session=Boom())
        with self.assertRaises(TelegramError) as ctx:
            api.call("getUpdates")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIsNone(ctx.exception.__context__)
        self.assertNotIn("SECRET", "".join(traceback.format_exception(ctx.exception)))
        self.assertNotIn("SECRET", repr(api))


if __name__ == "__main__":
    unittest.main()


class DryRunTest(unittest.TestCase):
    def test_dry_run_touches_no_repo_files(self):
        import contextlib
        import io

        import bot as bot_module
        from bot import DryRunAPI, _dry_run_store

        repo_files = [bot_module.STATE_FILE, bot_module.SUBSCRIBERS_FILE]
        before = [f.read_bytes() if f.exists() else None for f in repo_files]
        store, snapshot = _dry_run_store()
        self.assertNotEqual(store.state_file.parent, bot_module.ROOT)
        updates = [msg(1, "/start"), cb(2, "agree"), cb(3, "n:соня"), msg(4, "/tomorrow")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            b = Bot(DryRunAPI(updates), store, NameBook.load(), lambda: SCHEDULE,
                    lambda: datetime(2026, 11, 11, 21, 5), snapshot_file=snapshot)
            b.poll()
            b.check_changes()
            b.send_daily()
        printed = out.getvalue()
        self.assertIn("[dry-run] сообщение в chat 42", printed)
        self.assertIn("[Согласен]", printed)
        self.assertIn("РАСПИСАНИЕ РЕПЕТИЦИЙ 12.11.26:", printed)
        self.assertEqual(store.subscribers, {"42": "Соня"})       # только во временной копии
        after = [f.read_bytes() if f.exists() else None for f in repo_files]
        self.assertEqual(before, after)
