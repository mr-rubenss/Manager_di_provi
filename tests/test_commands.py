"""Общие команды: /now, /at, /who, /room — работают без регистрации."""

import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from bot import AWAIT_WHO_TTL, Bot, Store, parse_ddmm, parse_hhmm, running_at
from names import NameBook
from schedule_parser import Rehearsal, parse_participants

MSK = timezone(timedelta(hours=3))
TODAY = date(2026, 11, 16)
TOMORROW = date(2026, 11, 17)


def reh(title, people, start, end, room="301", day=TODAY):
    return Rehearsal(date=day, weekday="", start=start, end=end, time_raw="",
                     title=title, room=room, participants=parse_participants(people))


SCHEDULE = [
    reh("Триумф", "Софа, Ева", time(15), time(16)),
    reh("Эрнест", "Ди, Мира, Тихон", time(15, 30), time(16, 30), "309"),
    reh("Хелоу Долли", "Софа, Миша К", time(16), time(17)),           # 16:00 — уже не Триумф
    reh("Альба", "Лиза, Вася К, Вася О", time(16), time(17), "309"),
    reh("Без конца", "Гриша", time(15), None),                       # длительность неизвестна
    reh("Хор", "хор (женщины)", time(18), time(19)),
    reh("Сансет", "", time(18), time(19), "309"),                     # без участников
    reh("Карусель", "весь курс", time(12), time(13), day=TOMORROW),
    reh("110 в тени", "Миша П, Софа", time(17), time(18), "309", day=TOMORROW),
    reh("Оклахома", "Василиса, Лина", time(19), time(20), day=TOMORROW),
    reh("Далеко", "Соня", time(10), time(11), day=date(2026, 11, 20)),
]


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.updates = []

    def call(self, method, **params):
        if method == "getUpdates":
            res, self.updates = self.updates, []
            return [u for u in res if u["update_id"] >= params["offset"]]
        return True

    def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))


class Env:
    def __init__(self, now=datetime(2026, 11, 16, 15, 45), schedule=SCHEDULE):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(tmp / "state.json", tmp / "subs.json")
        self.api = FakeAPI()
        self.clock = {"now": now}
        self.bot = Bot(self.api, self.store, NameBook.load(), lambda: schedule,
                       lambda: self.clock["now"], snapshot_file=tmp / "snap.json")
        self.uid = 0
        self.tmp = tmp

    def send(self, text, chat=7, sent_at=None):
        """Сообщение от пользователя; sent_at — когда он его отправил (МСК)."""
        self.uid += 1
        msg = {"chat": {"id": chat, "type": "private"}, "text": text}
        if sent_at:
            msg["date"] = int(sent_at.replace(tzinfo=MSK).timestamp())
        self.api.updates = [{"update_id": self.uid, "message": msg}]
        self.api.sent.clear()
        self.bot.poll()
        return self.api.sent[-1]

    def press(self, data, chat=7):
        self.uid += 1
        self.api.updates = [{"update_id": self.uid, "callback_query": {
            "id": str(self.uid), "data": data,
            "message": {"chat": {"id": chat, "type": "private"}}}}]
        self.api.sent.clear()
        self.bot.poll()
        return self.api.sent[-1]


def buttons(kb):
    return [b["text"] for row in kb for b in row]


class ParseArgsTest(unittest.TestCase):
    def test_time(self):
        self.assertEqual(parse_hhmm("16:30"), time(16, 30))
        self.assertEqual(parse_hhmm("9.05"), time(9, 5))
        for bad in ("25:00", "16:60", "1630", "завтра", ""):
            self.assertIsNone(parse_hhmm(bad), bad)

    def test_date(self):
        self.assertEqual(parse_ddmm("17.11", TODAY), date(2026, 11, 17))
        self.assertEqual(parse_ddmm("1.12.26", TODAY), date(2026, 12, 1))
        self.assertEqual(parse_ddmm("01.01.2027", TODAY), date(2027, 1, 1))
        for bad in ("31.02", "17/11", "17.11.3", "301"):
            self.assertIsNone(parse_ddmm(bad, TODAY), bad)

    def test_running_at_bounds(self):
        titles = lambda t: sorted(r.title for r in running_at(SCHEDULE, TODAY, t))  # noqa: E731
        self.assertEqual(titles(time(15)), ["Триумф"])                  # начало включено
        self.assertEqual(titles(time(16)), ["Альба", "Хелоу Долли", "Эрнест"])  # конец — нет
        self.assertEqual(titles(time(14, 59)), [])
        self.assertNotIn("Без конца", titles(time(15, 10)))            # без конца не угадываем


class NowAtTest(unittest.TestCase):
    def test_now(self):
        env = Env()
        _, text, kb = env.send("/now")
        self.assertIsNone(kb)
        self.assertEqual(text,
                         "РЕПЕТИЦИИ 16.11.26 В 15:45:\n"
                         "15:00-16:00 Триумф (задействованы: Софа, Ева; аудитория 301)\n"
                         "15:30-16:30 Эрнест (задействованы: Ди, Мира, Тихон; аудитория 309)")

    def test_now_uses_time_message_was_sent(self):
        env = Env(now=datetime(2026, 11, 16, 17, 40))        # бот отвечает позже
        _, text, _ = env.send("/now", sent_at=datetime(2026, 11, 16, 15, 45))
        self.assertIn("В 15:45", text)
        self.assertIn("Триумф", text)
        self.assertNotIn("Хор", text)

    def test_now_nothing(self):
        env = Env(now=datetime(2026, 11, 16, 9, 0))
        self.assertEqual(env.send("/now")[1], "В это время репетиций нет")

    def test_at(self):
        env = Env()
        text = env.send("/at 16:15")[1]
        self.assertTrue(text.startswith("РЕПЕТИЦИИ 16.11.26 В 16:15:"))
        for title in ("Эрнест", "Хелоу Долли", "Альба"):                # параллельные — все
            self.assertIn(title, text)
        self.assertNotIn("Триумф", text)

    def test_at_with_date_and_order(self):
        env = Env()
        for cmd in ("/at 17:30 17.11", "/at 17.11 17:30", "/at 17.30 17.11.26"):
            with self.subTest(cmd=cmd):
                text = env.send(cmd)[1]
                self.assertEqual(text, "РЕПЕТИЦИИ 17.11.26 В 17:30:\n17:00-18:00 110 в тени "
                                       "(задействованы: Миша П, Софа; аудитория 309)")

    def test_at_nothing_and_errors(self):
        env = Env()
        self.assertEqual(env.send("/at 23:00")[1], "В это время репетиций нет")
        self.assertEqual(env.send("/at 12:00 20.11")[1], "В это время репетиций нет")
        for bad in ("/at", "/at завтра", "/at 25:00", "/at 16:00 17.11 лишнее"):
            with self.subTest(cmd=bad):
                self.assertIn("Напиши время так", env.send(bad)[1])

    def test_empty_participants_line(self):
        text = Env().send("/at 18:30")[1]
        self.assertIn("18:00-19:00 Сансет (задействованы: —; аудитория 309)", text)
        self.assertIn("Хор (задействованы: хор (женщины); аудитория 301)", text)


class WhoTest(unittest.TestCase):
    def test_who_with_name(self):
        env = Env()
        _, text, kb = env.send("/who соня")
        self.assertIsNone(kb)
        self.assertEqual(text,
                         "РАСПИСАНИЕ Соня 16.11.26:\n"
                         "16:00-17:00 Альба (задействованы: Лиза, Вася К, Вася О; аудитория 309)\n"
                         "18:00-19:00 Хор (задействованы: хор (женщины); аудитория 301)\n"
                         "\n"
                         "РАСПИСАНИЕ Соня 17.11.26:\n"
                         "12:00-13:00 Карусель (задействованы: весь курс; аудитория 301)")

    def test_who_spelling_and_aliases(self):
        env = Env()
        for typed, name in (("Лиза", "Соня"), ("ФЁДОР", "Фёдор"), ("федя", "Фёдор"),
                            ("Вася К", "Василиса К."), ("ди", "Дианна"), ("Миша п", "Миша П")):
            with self.subTest(typed=typed):
                self.assertTrue(env.send(f"/who {typed}")[1].startswith(f"РАСПИСАНИЕ {name} "))

    def test_who_groups_by_gender(self):
        env = Env()
        self.assertIn("Хор", env.send("/who Лина")[1])         # девушка: «хор (женщины)»
        tikhon = env.send("/who Тихон")[1]
        self.assertNotIn("Хор", tikhon)
        self.assertIn("Карусель", tikhon)                        # «весь курс» — всем

    def test_who_empty_days(self):
        text = Env().send("/who Арсений")[1]
        self.assertEqual(text, "РАСПИСАНИЕ Арсений 16.11.26:\nрепетиций нет\n\n"
                               "РАСПИСАНИЕ Арсений 17.11.26:\n12:00-13:00 Карусель "
                               "(задействованы: весь курс; аудитория 301)")

    def test_who_distinct_people(self):
        env = Env()
        k = env.send("/who Миша К")[1]
        p = env.send("/who Миша П")[1]
        self.assertIn("Хелоу Долли", k)
        self.assertNotIn("110 в тени", k)
        self.assertIn("110 в тени", p)
        self.assertNotIn("Хелоу Долли", p)
        self.assertNotIn("Альба", env.send("/who Софа")[1])         # Софа != Соня/Лиза
        # «Василиса» без буквы в таблице — не показываем ни К., ни О
        self.assertNotIn("Оклахома", env.send("/who Василиса К.")[1])
        self.assertNotIn("Оклахома", env.send("/who Василиса О")[1])

    def test_ambiguous_shows_buttons(self):
        env = Env()
        for typed, options in (("Миша", ["Миша К", "Миша П"]),
                               ("Вася", ["Василиса К.", "Василиса О"]),
                               ("василиса", ["Василиса К.", "Василиса О"])):
            with self.subTest(typed=typed):
                _, text, kb = env.send(f"/who {typed}")
                self.assertIn("нескольких человек", text)
                self.assertEqual(buttons(kb), options)
        self.assertTrue(env.press("w:миша п")[1].startswith("РАСПИСАНИЕ Миша П 16.11.26:"))

    def test_unknown_name(self):
        text = Env().send("/who Петя")[1]
        self.assertIn("Не нашёл такого имени", text)

    def test_who_without_name_buttons_and_next_message(self):
        env = Env()
        _, text, kb = env.send("/who")
        names = buttons(kb)
        self.assertEqual(len(names), len(set(names)))                # без повторов
        for n in ("Соня", "Софа", "Миша К", "Миша П", "Василиса К.", "Лина", "Света"):
            self.assertIn(n, names)
        for n in ("Лиза", "Вася К", "Федя", "Миша", "весь курс"):
            self.assertNotIn(n, names)
        state = json.loads((env.tmp / "state.json").read_text())
        self.assertIn("7", state["awaiting_who"])                    # жду имя
        # следующее сообщение — имя для /who, а не регистрация
        self.assertTrue(env.send("Тихон")[1].startswith("РАСПИСАНИЕ Тихон 16.11.26:"))
        self.assertEqual(env.store.subscribers, {})
        self.assertNotIn("awaiting_who", json.loads((env.tmp / "state.json").read_text()))
        # ожидание одноразовое: дальше текст снова считается регистрацией
        self.assertIn("ты записан(а) как «Тихон»", env.send("Тихон")[1])

    def test_who_button_press(self):
        env = Env()
        env.send("/who")
        self.assertTrue(env.press("w:софа")[1].startswith("РАСПИСАНИЕ Софа 16.11.26:"))
        self.assertNotIn("awaiting_who", env.store.state)

    def test_awaiting_is_per_chat_and_cleared_by_command(self):
        env = Env()
        env.send("/who", chat=1)
        self.assertIn("ты записан(а) как «Ева»", env.send("Ева", chat=2)[1])   # чужой чат
        env.send("/now", chat=1)                                     # другая команда
        self.assertIn("ты записан(а) как «Ева»", env.send("Ева", chat=1)[1])

    def test_awaiting_expires(self):
        env = Env()
        env.send("/who")
        env.clock["now"] += AWAIT_WHO_TTL + timedelta(minutes=1)
        self.assertIn("ты записан(а)", env.send("Соня")[1])

    def test_expired_entries_removed_even_without_messages(self):
        env = Env()
        env.send("/who", chat=1)
        env.clock["now"] += AWAIT_WHO_TTL + timedelta(minutes=1)
        env.api.updates = []
        env.bot.poll()                                   # пустой запуск, сообщений нет
        self.assertNotIn("awaiting_who", json.loads((env.tmp / "state.json").read_text()))

    def test_ambiguous_name_after_who(self):
        env = Env()
        env.send("/who")
        _, text, kb = env.send("Миша")
        self.assertEqual(buttons(kb), ["Миша К", "Миша П", "Назад в меню"])
        self.assertEqual(env.store.subscribers, {})


class RoomTest(unittest.TestCase):
    def test_buttons_from_table(self):
        _, text, kb = Env().send("/room")
        self.assertEqual(buttons(kb), ["301", "309"])
        self.assertEqual([b["callback_data"] for row in kb for b in row], ["r:301", "r:309"])

    def test_room_today_and_tomorrow(self):
        text = Env().send("/room 309")[1]
        self.assertEqual(text,
                         "АУДИТОРИЯ 309 16.11.26:\n"
                         "15:30-16:30 Эрнест (задействованы: Ди, Мира, Тихон; аудитория 309)\n"
                         "16:00-17:00 Альба (задействованы: Лиза, Вася К, Вася О; аудитория 309)\n"
                         "18:00-19:00 Сансет (задействованы: —; аудитория 309)\n"
                         "\n"
                         "АУДИТОРИЯ 309 17.11.26:\n"
                         "17:00-18:00 110 в тени (задействованы: Миша П, Софа; аудитория 309)")

    def test_room_with_date(self):
        env = Env()
        self.assertEqual(env.send("/room 301 20.11")[1],
                         "АУДИТОРИЯ 301 20.11.26:\n"
                         "10:00-11:00 Далеко (задействованы: Соня; аудитория 301)")
        self.assertEqual(env.send("/room 309 20.11")[1], "АУДИТОРИЯ 309 20.11.26:\nрепетиций нет")
        self.assertIn("АУДИТОРИЯ 301 20.11.26:", env.send("/room 20.11 301")[1])

    def test_room_date_only_gives_buttons_with_date(self):
        env = Env()
        _, _, kb = env.send("/room 17.11")
        self.assertEqual([b["callback_data"] for row in kb for b in row],
                         ["r:301:2026-11-17", "r:309:2026-11-17"])
        text = env.press("r:309:2026-11-17")[1]
        self.assertEqual(text, "АУДИТОРИЯ 309 17.11.26:\n17:00-18:00 110 в тени "
                               "(задействованы: Миша П, Софа; аудитория 309)")

    def test_room_button(self):
        self.assertTrue(Env().press("r:301")[1].startswith("АУДИТОРИЯ 301 16.11.26:"))

    def test_unknown_room(self):
        self.assertEqual(Env().send("/room 205")[1],
                         "Аудитории «205» нет в расписании. Есть: 301, 309.")


class NoRegistrationNeededTest(unittest.TestCase):
    def test_commands_dont_register_anyone(self):
        env = Env()
        for cmd in ("/now", "/at 16:00", "/who Соня", "/who", "/room", "/room 301"):
            env.send(cmd, chat=99)
        self.assertEqual(env.store.subscribers, {})
        self.assertEqual(json.loads((env.tmp / "subs.json").read_text()), {})

    def test_help_lists_commands(self):
        text = Env().send("/help")[1]
        for cmd in ("/now", "/at ЧЧ:ММ [дд.мм]", "/who [имя]", "/room [номер] [дд.мм]"):
            self.assertIn(cmd, text)


if __name__ == "__main__":
    unittest.main()
