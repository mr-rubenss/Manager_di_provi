import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

import changes as ch
from bot import Bot, Store
from names import NameBook
from schedule_parser import Rehearsal, parse_participants

TODAY = date(2026, 11, 11)
D1 = date(2026, 11, 12)
D2 = date(2026, 11, 13)


def reh(title, people, start, end, room="301", day=D1):
    return Rehearsal(date=day, weekday="", start=start, end=end, time_raw="",
                     title=title, room=room, participants=parse_participants(people))


BASE = [
    reh("Альба", "Соня, Миша К", time(12), time(13), "309"),
    reh("110 в тени", "Софа, Вася О", time(11), time(12)),
    reh("Эрнест", "Мира, Миша П", time(11), time(12)),            # параллельно с «110 в тени»
    reh("Клоп", "Федя, Тома", time(14), time(15), day=D2),
    reh("Прошлое", "Соня", time(10), time(11), day=date(2026, 11, 10)),
]


class FakeAPI:
    def __init__(self):
        self.sent = []

    def call(self, method, **params):
        return [] if method == "getUpdates" else True

    def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))


class Env:
    """Бот с подменяемой таблицей и временными файлами."""

    def __init__(self, subscribers):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "schedule_snapshot.json"
        self.table = list(BASE)
        self.api = FakeAPI()
        self.store = Store(self.tmp / "state.json", self.tmp / "subscribers.json")
        self.store.subscribers = dict(subscribers)

    def run(self, loader=None):
        """Один запуск poll: новый Bot (как в GitHub Actions), свежая загрузка."""
        self.api.sent.clear()
        bot = Bot(self.api, self.store, NameBook.load(), loader or (lambda: list(self.table)),
                  lambda: datetime.combine(TODAY, time(15)), snapshot_file=self.snapshot)
        bot.check_changes()
        return {c: t for c, t in self.api.sent}

    def snapshot_titles(self):
        return sorted(r["title"] for r in json.loads(self.snapshot.read_text())["records"])


SUBS = {"1": "Соня", "2": "Софа", "3": "Миша К", "4": "Миша П", "5": "Фёдор", "6": "Вася К"}


class SnapshotTest(unittest.TestCase):
    def test_first_run_only_creates_snapshot(self):
        env = Env(SUBS)
        self.assertEqual(env.run(), {})
        self.assertTrue(env.snapshot.exists())
        # прошлые записи в снимок не попадают
        self.assertEqual(env.snapshot_titles(), ["110 в тени", "Альба", "Клоп", "Эрнест"])

    def test_no_changes_no_messages(self):
        env = Env(SUBS)
        env.run()
        self.assertEqual(env.run(), {})

    def test_download_failure_keeps_snapshot(self):
        env = Env(SUBS)
        env.run()
        before = env.snapshot.read_text()

        def boom():
            raise ConnectionError("network down")

        for loader in (boom, lambda: []):
            self.assertEqual(env.run(loader), {})
            self.assertEqual(env.snapshot.read_text(), before)
        # таблица снова доступна и не менялась — ложных уведомлений нет
        self.assertEqual(env.run(), {})

    def test_cosmetic_edits_are_not_changes(self):
        env = Env(SUBS)
        env.run()
        env.table[0] = reh("альба", "миша к ,  Лиза", time(12), time(13), "309")
        self.assertEqual(env.run(), {})


class NotifyTest(unittest.TestCase):
    def test_time_change_goes_to_participants_only(self):
        env = Env(SUBS)
        env.run()
        env.table[0] = reh("Альба", "Соня, Миша К", time(12, 30), time(13, 30), "309")
        sent = env.run()
        self.assertEqual(set(sent), {1, 3})
        self.assertEqual(sent[1],
                         "ИЗМЕНЕНИЕ В РАСПИСАНИИ 12.11.26:\n"
                         "было: 12:00-13:00 Альба (задействованы: Соня, Миша К; аудитория 309)\n"
                         "стало: 12:30-13:30 Альба (задействованы: Соня, Миша К; аудитория 309)")
        self.assertEqual(sent[3], sent[1])
        # снимок обновлён — повторно не шлём
        self.assertEqual(env.run(), {})

    def test_old_and_new_cast_notified(self):
        env = Env(SUBS)
        env.run()
        env.table[2] = reh("Эрнест", "Мира, Миша К", time(11), time(12))  # Миша П -> Миша К
        sent = env.run()
        self.assertEqual(set(sent), {3, 4})                 # и убранный, и добавленный
        self.assertIn("было: 11:00-12:00 Эрнест (задействованы: Мира, Миша П; аудитория 301)",
                      sent[4])
        self.assertIn("стало: 11:00-12:00 Эрнест (задействованы: Мира, Миша К; аудитория 301)",
                      sent[4])

    def test_room_change(self):
        env = Env(SUBS)
        env.run()
        env.table[1] = reh("110 в тени", "Софа, Вася О", time(11), time(12), "309")
        sent = env.run()
        self.assertEqual(set(sent), {2})                    # Софа, но не Соня
        self.assertIn("аудитория 309)", sent[2].splitlines()[2])

    def test_added_and_removed(self):
        env = Env(SUBS)
        env.run()
        del env.table[3]                                     # «Клоп» 13.11 удалили
        env.table.append(reh("Войцек", "Вася К, Лиза", time(16), time(16, 30), day=D2))
        sent = env.run()
        self.assertEqual(set(sent), {1, 5, 6})
        self.assertEqual(sent[5],
                         "ИЗМЕНЕНИЕ В РАСПИСАНИИ 13.11.26:\n"
                         "было: 14:00-15:00 Клоп (задействованы: Федя, Тома; аудитория 301)\n"
                         "стало: отменено")
        self.assertEqual(sent[6],
                         "ИЗМЕНЕНИЕ В РАСПИСАНИИ 13.11.26:\n"
                         "было: —\n"
                         "стало: 16:00-16:30 Войцек (задействованы: Вася К, Лиза; аудитория 301)")

    def test_several_changes_one_message(self):
        env = Env(SUBS)
        env.run()
        env.table[0] = reh("Альба", "Соня, Миша К", time(12), time(13), "301")
        env.table.append(reh("Новая", "Лиза", time(10), time(11), day=D2))
        sent = env.run()
        self.assertEqual(len([c for c, _ in env.api.sent if c == 1]), 1)  # одно сообщение
        text = sent[1]
        self.assertIn("ИЗМЕНЕНИЕ В РАСПИСАНИИ 12.11.26:", text)
        self.assertIn("ИЗМЕНЕНИЕ В РАСПИСАНИИ 13.11.26:", text)
        self.assertLess(text.index("12.11.26"), text.index("13.11.26"))

    def test_parallel_twin_added(self):
        env = Env(SUBS)
        env.run()
        env.table.append(reh("Эрнест", "Мира, Миша П", time(11), time(12)))  # копия параллельно
        sent = env.run()
        self.assertEqual(set(sent), {4})
        self.assertEqual(sent[4].splitlines()[1], "было: —")

    def test_past_changes_ignored(self):
        env = Env(SUBS)
        env.run()
        env.table[4] = reh("Прошлое", "Соня, Софа", time(9), time(10), day=date(2026, 11, 10))
        self.assertEqual(env.run(), {})

    def test_record_dropping_into_past_is_not_removal(self):
        env = Env(SUBS)
        env.run()
        # тот же снимок, но «сегодня» уже 13.11: записи 12.11 ушли в прошлое
        bot = Bot(env.api, env.store, NameBook.load(), lambda: list(env.table),
                  lambda: datetime(2026, 11, 13, 9), snapshot_file=env.snapshot)
        env.api.sent.clear()
        bot.check_changes()
        self.assertEqual(env.api.sent, [])


class DiffUnitTest(unittest.TestCase):
    def test_title_change_paired_by_slot(self):
        book = NameBook.load()
        old = [reh("Альба", "Соня", time(12), time(13))]
        new = [reh("Дом Бернарды Альбы", "Соня", time(12), time(13))]
        changes = ch.diff(old, new, book)
        self.assertEqual(len(changes), 1)
        self.assertIsNotNone(changes[0].old)
        self.assertIsNotNone(changes[0].new)

    def test_snapshot_roundtrip(self):
        r = reh("Клоп", "Софа, (Василиса?)", time(16, 15), time(16, 35))
        self.assertEqual(ch.from_dict(json.loads(json.dumps(ch.to_dict(r)))), r)


if __name__ == "__main__":
    unittest.main()
