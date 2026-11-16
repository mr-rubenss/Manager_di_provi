"""/help + меню команд, /next, /find, /free, /date, /mute и /unmute."""

import json
import unittest
from datetime import date, datetime, time, timedelta

import bot as bot_module
from bot import COMMANDS, HELP_TEXT, TELEGRAM_COMMANDS, Store, free_windows
from tests.test_commands import TODAY, TOMORROW, Env, reh

SCHEDULE = [
    reh("Триумф любви муз (Овчинникова)", "Соня, Тома", time(10), time(11)),
    reh("Эрнест драм", "Мира, Миша П", time(12), time(13)),
    reh("эрнест", "Мира, Миша П", time(15), time(16), "309"),
    reh("Альба", "Соня, Миша П", time(15), time(16)),             # параллельно с «эрнест»
    reh("Хор", "хор (женщины)", time(18), time(19)),
    reh("Без конца", "Гриша", time(20), None),
    reh("Ёлка", "Ева", time(21), time(21, 30)),
    reh("Эрнест", "Мира, Вася О", time(9), time(10), "309", day=TOMORROW),
    reh("Карусель", "весь курс", time(11), time(12), day=TOMORROW),
    reh("Альба", "Соня", time(12), time(13), "309", day=date(2026, 11, 20)),
    reh("Эрнест старое", "Мира", time(10), time(11), day=date(2026, 11, 10)),
]


def env(now=datetime(2026, 11, 16, 12, 30), subs=None):
    e = Env(now=now, schedule=SCHEDULE)
    e.store.subscribers.update(subs or {"7": "Миша П"})
    return e


class HelpTest(unittest.TestCase):
    def test_help_lists_every_command(self):
        text = env().send("/help")[1]
        self.assertEqual(text, HELP_TEXT)
        for cmd, args, desc in COMMANDS:
            self.assertIn(f"/{cmd}{' ' + args if args else ''} — {desc}", text)
        for cmd in ("/next", "/find название", "/free [аудитория] [дд.мм]", "/date дд.мм",
                    "/mute", "/unmute", "/who [имя]", "/room [номер] [дд.мм]", "/stop"):
            self.assertIn(cmd, text)

    def test_unknown_command_shows_help(self):
        self.assertEqual(env().send("/abracadabra")[1], HELP_TEXT)

    def test_telegram_menu(self):
        for c in TELEGRAM_COMMANDS:
            self.assertRegex(c["command"], r"^[a-z0-9_]{1,32}$")      # требования Telegram
            self.assertTrue(3 <= len(c["description"]) <= 256)
        self.assertEqual([c["command"] for c in TELEGRAM_COMMANDS], [c for c, _, _ in COMMANDS])

    def test_set_my_commands_called_once(self):
        e = env()
        calls = []
        orig = e.api.call
        e.api.call = lambda m, **p: calls.append((m, p)) or orig(m, **p)
        e.bot.poll()
        e.bot.poll()
        menu = [p for m, p in calls if m == "setMyCommands"]
        self.assertEqual(len(menu), 1)                                 # второй раз — не нужно
        self.assertEqual(menu[0]["commands"], TELEGRAM_COMMANDS)
        self.assertIn("commands_hash", json.loads((e.tmp / "state.json").read_text()))

    def test_set_my_commands_retried_after_error(self):
        e = env()
        calls = []

        def failing(method, **params):
            calls.append(method)
            if method == "setMyCommands":
                raise bot_module.TelegramError("boom")
            return []
        e.api.call = failing
        e.bot.poll()
        e.bot.poll()
        self.assertEqual(calls.count("setMyCommands"), 2)
        self.assertNotIn("commands_hash", e.store.state)


class NextTest(unittest.TestCase):
    def test_next_today(self):
        text = env(now=datetime(2026, 11, 16, 13, 30)).send("/next")[1]
        self.assertEqual(text, "БЛИЖАЙШАЯ РЕПЕТИЦИЯ СЕГОДНЯ:\n"
                               "15:00-16:00 эрнест (задействованы: Мира, Миша П; аудитория 309)\n"
                               "15:00-16:00 Альба (задействованы: Соня, Миша П; аудитория 301)")

    def test_running_now(self):
        text = env(now=datetime(2026, 11, 16, 12, 10)).send("/next")[1]
        self.assertTrue(text.startswith("СЕЙЧАС ИДЁТ:\n12:00-13:00 Эрнест драм"))

    def test_next_with_date_when_not_today(self):
        text = env(now=datetime(2026, 11, 16, 17, 0)).send("/next")[1]
        self.assertEqual(text, "БЛИЖАЙШАЯ РЕПЕТИЦИЯ 17.11.26:\n"
                               "11:00-12:00 Карусель (задействованы: весь курс; аудитория 301)")

    def test_next_includes_groups(self):
        e = env(now=datetime(2026, 11, 16, 16, 30), subs={"7": "Лина"})
        self.assertIn("Хор", e.send("/next")[1])

    def test_next_uses_send_time(self):
        e = env(now=datetime(2026, 11, 16, 17, 0))
        text = e.send("/next", sent_at=datetime(2026, 11, 16, 14, 0))[1]
        self.assertIn("СЕГОДНЯ", text)
        self.assertIn("15:00-16:00", text)

    def test_nothing_and_unregistered(self):
        e = env(now=datetime(2026, 11, 25, 9, 0))
        self.assertEqual(e.send("/next")[1], "Миша П, ближайших репетиций нет.")
        self.assertIn("Сначала напиши мне своё имя", env().send("/next", chat=99)[1])


class FindTest(unittest.TestCase):
    def test_find_substring_case_insensitive(self):
        e = env()
        text = e.send("/find ЭРН")[1]
        self.assertEqual(text,
                         "НАЙДЕНО «ЭРН»:\n\n"
                         "16.11.26:\n"
                         "12:00-13:00 Эрнест драм (задействованы: Мира, Миша П; аудитория 301)\n"
                         "15:00-16:00 эрнест (задействованы: Мира, Миша П; аудитория 309)\n\n"
                         "17.11.26:\n"
                         "09:00-10:00 Эрнест (задействованы: Мира, Вася О; аудитория 309)")
        self.assertNotIn("старое", text)                               # прошлое не ищем

    def test_find_skips_ended_today(self):
        text = env(now=datetime(2026, 11, 16, 13, 0)).send("/find эрнест")[1]
        self.assertNotIn("Эрнест драм", text)                          # закончилась в 13:00
        self.assertIn("15:00-16:00 эрнест", text)

    def test_find_words_any_order_and_yo(self):
        e = env(now=datetime(2026, 11, 16, 9, 0))
        self.assertIn("Триумф любви муз", e.send("/find триумф муз")[1])
        self.assertIn("Триумф любви муз", e.send("/find муз триумф")[1])
        self.assertIn("Ёлка", e.send("/find елка")[1])

    def test_find_works_without_registration(self):
        self.assertIn("Альба", env().send("/find альба", chat=99)[1])

    def test_find_nothing_and_empty(self):
        e = env()
        self.assertEqual(e.send("/find гамлет")[1], "Ничего не нашёл по «гамлет» на ближайшие дни.")
        self.assertIn("Напиши часть названия", e.send("/find")[1])

    def test_find_limit(self):
        many = [reh("Эрнест", "Мира", time(10), time(11), day=TODAY + timedelta(days=i))
                for i in range(15)]
        e = Env(now=datetime(2026, 11, 16, 9, 0), schedule=many)
        text = e.send("/find эрнест")[1]
        self.assertEqual(text.count("10:00-11:00 Эрнест"), 10)
        self.assertIn("и ещё 5", text)


class FreeTest(unittest.TestCase):
    def test_windows(self):
        windows, unclear = free_windows("301", TODAY, SCHEDULE)
        self.assertEqual(windows, [(time(9), time(10)), (time(11), time(12)),
                                   (time(13), time(15)), (time(16), time(18)),
                                   (time(19), time(21)), (time(21, 30), time(22))])
        self.assertEqual([r.title for r in unclear], ["Без конца"])

    def test_overlaps_and_edges(self):
        sched = [reh("a", "Ева", time(8), time(9, 30)),             # начинается до рабочего дня
                 reh("b", "Ева", time(9), time(11)),
                 reh("c", "Ева", time(10), time(12)),               # пересекается с b
                 reh("d", "Ева", time(12), time(13)),               # встык с c
                 reh("e", "Ева", time(21), time(23))]               # заканчивается после
        self.assertEqual(free_windows("301", TODAY, sched)[0], [(time(13), time(21))])
        self.assertEqual(free_windows("309", TODAY, sched)[0], [(time(9), time(22))])

    def test_workday_setting(self):
        self.assertEqual(free_windows("309", TODAY, SCHEDULE, "08:00", "23:00")[0],
                         [(time(8), time(15)), (time(16), time(23))])

    def test_workday_env_parsing(self):
        import os
        from unittest import mock
        for value, expected in (("08:30", "08:30"), ("8:30", "08:30"), ("", "09:00"),
                                ("25:00", "09:00"), ("утро", "09:00")):
            with mock.patch.dict(os.environ, {"WORKDAY_START": value}):
                self.assertEqual(bot_module._workday("WORKDAY_START", "09:00"), expected, value)

    def test_free_command_room_today_and_tomorrow(self):
        text = env().send("/free 309")[1]
        self.assertEqual(text, "СВОБОДНО 309 16.11.26 (09:00–22:00):\n09:00-15:00\n16:00-22:00\n\n"
                               "СВОБОДНО 309 17.11.26 (09:00–22:00):\n10:00-22:00")

    def test_free_unclear_shown(self):
        text = env().send("/free 301 16.11")[1]
        self.assertIn("не учтены — нет времени окончания:\n20:00 Без конца", text)
        self.assertNotIn("17.11.26", text)

    def test_free_all_rooms(self):
        text = env().send("/free 20.11", chat=99)[1]                    # без регистрации
        self.assertIn("СВОБОДНО 301 20.11.26 (09:00–22:00):\n09:00-22:00", text)
        self.assertIn("СВОБОДНО 309 20.11.26 (09:00–22:00):\n09:00-12:00\n13:00-22:00", text)

    def test_free_errors(self):
        e = env()
        self.assertIn("Аудитории «205» нет", e.send("/free 205")[1])
        self.assertIn("Напиши так", e.send("/free 301 309")[1])


class DateTest(unittest.TestCase):
    def test_date(self):
        e = env(subs={"7": "Соня"})
        self.assertEqual(e.send("/date 20.11")[1],
                         "РАСПИСАНИЕ РЕПЕТИЦИЙ 20.11.26:\n"
                         "12:00-13:00 Альба (задействованы: Соня; аудитория 309)")
        self.assertIn("Карусель", e.send("/date 17.11.26")[1])           # группа «весь курс»
        self.assertEqual(e.send("/date 25.11")[1], "Соня, на 25.11.26 репетиций нет.")

    def test_date_errors(self):
        e = env()
        for bad in ("/date", "/date 31.02", "/date завтра", "/date 17.11 18.11"):
            with self.subTest(cmd=bad):
                self.assertEqual(e.send(bad)[1], "Напиши дату так: /date 18.11")
        self.assertIn("Сначала напиши мне своё имя", env().send("/date 17.11", chat=99)[1])


class MuteTest(unittest.TestCase):
    def test_mute_flag_in_file_and_unmute(self):
        e = env(subs={"7": "Соня"})
        self.assertIn("выключены", e.send("/mute")[1])
        data = json.loads((e.tmp / "subs.json").read_text())
        self.assertEqual(data, {"7": {"name": "Соня", "muted": True}})
        self.assertIn("уже выключены", e.send("/mute")[1])
        self.assertIn("снова включены", e.send("/unmute")[1])
        self.assertEqual(json.loads((e.tmp / "subs.json").read_text()), {"7": "Соня"})
        self.assertIn("уже включены", e.send("/unmute")[1])

    def test_old_and_new_format_load(self):
        e = env()
        (e.tmp / "subs.json").write_text(json.dumps(
            {"1": "Соня", "2": {"name": "Софа", "muted": True}}, ensure_ascii=False))
        st = Store(e.tmp / "state.json", e.tmp / "subs.json")
        self.assertEqual(st.subscribers, {"1": "Соня", "2": "Софа"})
        self.assertEqual(st.muted, {"2"})

    def test_muted_get_no_daily(self):
        e = env(now=datetime(2026, 11, 16, 21, 5), subs={"1": "Соня", "2": "Ева"})
        e.store.muted.add("1")
        e.bot.send_daily()
        self.assertEqual({c for c, *_ in e.api.sent}, {2})                 # «весь курс» завтра

    def test_muted_get_no_change_notifications(self):
        e = env(subs={"1": "Соня", "2": "Ева"})
        e.bot.check_changes()                                             # снимок
        e.store.muted.add("1")
        changed = list(SCHEDULE) + [reh("Новая", "Соня, Ева", time(17), time(18), day=TOMORROW)]
        e.bot._loader = lambda: changed
        e.bot._schedule = None
        e.api.sent.clear()
        e.bot.check_changes()
        self.assertEqual({c for c, *_ in e.api.sent}, {2})

    def test_muted_commands_still_work(self):
        e = env(subs={"7": "Соня"})
        e.send("/mute")
        self.assertIn("Альба", e.send("/date 20.11")[1])
        self.assertIn("БЛИЖАЙШАЯ", e.send("/next")[1])

    def test_rename_keeps_mute_stop_clears(self):
        e = env(subs={"7": "Соня"})
        e.send("/mute")
        e.send("Софа")                                                    # сменил имя
        self.assertEqual(json.loads((e.tmp / "subs.json").read_text()),
                         {"7": {"name": "Софа", "muted": True}})
        e.send("/stop")
        self.assertEqual(json.loads((e.tmp / "subs.json").read_text()), {})
        e.send("Софа")                                                    # снова записался
        self.assertEqual(json.loads((e.tmp / "subs.json").read_text()), {"7": "Софа"})

    def test_mute_needs_registration(self):
        self.assertIn("Сначала напиши мне своё имя", env().send("/mute", chat=99)[1])


if __name__ == "__main__":
    unittest.main()
