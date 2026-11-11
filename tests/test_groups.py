import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from bot import Bot, Store, format_line
from names import ALL, FEMALE, MALE, PEOPLE_FILE, NameBook, group_of
from schedule_parser import Participant, Rehearsal, parse_participants

D = date(2026, 11, 12)


def reh(title, people, start=time(12), end=time(13), room="301", day=D):
    return Rehearsal(date=day, weekday="", start=start, end=end, time_raw="",
                     title=title, room=room, participants=parse_participants(people))


# подписчики: девушки, парни, пол «?» и имя не из people.json
SUBS = {"1": "Соня", "2": "Дианна", "3": "Тихон", "4": "Фёдор", "5": "Даня", "6": "Мира"}
GIRLS, BOYS, UNSURE = {1, 2}, {3, 4}, {5, 6}
EVERYONE = GIRLS | BOYS | UNSURE


class FakeAPI:
    def __init__(self):
        self.sent = []

    def call(self, method, **params):
        return [] if method == "getUpdates" else True

    def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))


def daily_recipients(table, subs=SUBS):
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "s.json", tmp / "subs.json")
    store.subscribers = dict(subs)
    api = FakeAPI()
    Bot(api, store, NameBook.load(), lambda: table, lambda: datetime(2026, 11, 11, 21),
        snapshot_file=tmp / "snap.json").send_daily()
    return {c: t for c, t in api.sent}


class AliasesTest(unittest.TestCase):
    def test_new_aliases(self):
        book = NameBook.load()
        self.assertEqual(book.key("Ди"), book.key("Дианна"))
        self.assertEqual(book.key("тима"), book.key("Тимофей"))
        self.assertTrue(book.matches("Дианна", reh("x", "Ди, Мира")))
        self.assertTrue(book.matches("Тимофей", reh("x", "тима")))
        names = book.registration_names([reh("x", "Ди, Дианна, Тима, Тимофей")])
        self.assertEqual(names, ["Дианна", "Тимофей"])


class GroupOfTest(unittest.TestCase):
    def test_phrases(self):
        cases = {
            "весь курс": ALL, "Все": ALL, "хор": ALL, "ВЕСЬ  КУРС": ALL,
            "все девушки": FEMALE, "Девушки": FEMALE, "хор женщины": FEMALE,
            "хор (женщины)": FEMALE, "хор(женщины)": FEMALE,
            "парни": MALE, "хор мужчины": MALE, "хор (мужчины)": MALE,
            "Соня": None, "хорошо": None, "всё": ALL,  # ё == е
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                (p,) = parse_participants(raw)
                self.assertEqual(group_of(p), expected)


class GroupDeliveryTest(unittest.TestCase):
    def test_all(self):
        for group in ("весь курс", "все", "хор"):
            with self.subTest(group=group):
                self.assertEqual(set(daily_recipients([reh("Карусель", group)])), EVERYONE)

    def test_girls_only(self):
        for group in ("все девушки", "девушки", "хор женщины", "хор (женщины)"):
            with self.subTest(group=group):
                self.assertEqual(set(daily_recipients([reh("Хор", group)])), GIRLS)

    def test_boys_only(self):
        for group in ("парни", "хор мужчины", "хор (мужчины)"):
            with self.subTest(group=group):
                self.assertEqual(set(daily_recipients([reh("Хор", group)])), BOYS)

    def test_unknown_gender_gets_only_all(self):
        book = NameBook.load()
        for name in ("Даня", "Мира", "Вася К", "Дея"):
            self.assertIsNone(book.gender(name))
            self.assertTrue(book.matches(name, reh("x", "весь курс")))
            self.assertFalse(book.matches(name, reh("x", "девушки")))
            self.assertFalse(book.matches(name, reh("x", "парни")))

    def test_group_plus_names(self):
        # девушкам + конкретный парень по имени
        got = set(daily_recipients([reh("Хор", "девушки, Тихон")]))
        self.assertEqual(got, GIRLS | {3})

    def test_group_shown_as_written(self):
        r = reh("Хор", "хор (женщины), Тихон")
        self.assertEqual(format_line(r),
                         "12:00-13:00 Хор (задействованы: хор (женщины), Тихон; аудитория 301)")
        text = daily_recipients([reh("Карусель", "весь курс, хор")])[1]
        self.assertIn("(задействованы: весь курс, хор; аудитория 301)", text)


class UnknownWordsTest(unittest.TestCase):
    def setUp(self):
        self.book = NameBook.load()
        self.table = [reh("Клоп", "Соня, концертмейстер, Петя, Миша, Миша К")]

    def test_not_delivered(self):
        self.assertFalse(self.book.matches("концертмейстер", self.table[0]))
        self.assertFalse(self.book.matches("Петя", self.table[0]))
        # а известные из той же записи получают
        self.assertTrue(self.book.matches("Соня", self.table[0]))

    def test_reported(self):
        unknown = [m.name for m in self.book.unknown(self.table)]
        self.assertEqual(unknown, ["концертмейстер", "Петя"])   # «Миша» — неоднозначное, не тут
        self.assertEqual([m.name for m in self.book.ambiguous(self.table)], ["Миша"])

    def test_not_in_registration(self):
        names = self.book.registration_names(self.table + [reh("x", "весь курс, девушки")])
        self.assertEqual(names, ["Миша К", "Соня"])


class PeopleFileTest(unittest.TestCase):
    def test_values_valid(self):
        data = json.loads(PEOPLE_FILE.read_text(encoding="utf-8"))
        self.assertTrue(set(data.values()) <= {"ж", "м", "?"})
        book = NameBook.load()
        # варианты из aliases.json в people.json не дублируются
        self.assertEqual(len({book.key(n) for n in data}), len(data))

    def test_distinct_people_still_distinct(self):
        book = NameBook.load()
        for a, b in [("Миша К", "Миша П"), ("Вася О", "Вася К"), ("Софа", "Соня"),
                     ("Софа", "Лиза"), ("Ди", "Дея")]:
            self.assertNotEqual(book.key(a), book.key(b))

    def test_bad_gender_rejected(self):
        with self.assertRaises(ValueError):
            NameBook({}, {"Соня": "девочка"})


class GroupChangesTest(unittest.TestCase):
    def run_twice(self, before, after):
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "s.json", tmp / "subs.json")
        store.subscribers = dict(SUBS)
        api = FakeAPI()
        for table in (before, after):
            api.sent.clear()
            Bot(api, store, NameBook.load(), lambda t=table: t,
                lambda: datetime(2026, 11, 11, 15), snapshot_file=tmp / "snap.json").check_changes()
        return {c: t for c, t in api.sent}

    def test_change_of_all_group_rehearsal(self):
        sent = self.run_twice([reh("Карусель", "весь курс")],
                              [reh("Карусель", "весь курс", time(13), time(14))])
        self.assertEqual(set(sent), EVERYONE)
        self.assertIn("было: 12:00-13:00 Карусель (задействованы: весь курс; аудитория 301)",
                      sent[5])

    def test_girls_to_boys_notifies_both(self):
        sent = self.run_twice([reh("Хор", "хор (женщины)")], [reh("Хор", "хор (мужчины)")])
        self.assertEqual(set(sent), GIRLS | BOYS)               # «?» не трогаем
        self.assertIn("стало: 12:00-13:00 Хор (задействованы: хор (мужчины); аудитория 301)",
                      sent[1])

    def test_added_for_girls(self):
        sent = self.run_twice([reh("Альба", "Тихон")],
                              [reh("Альба", "Тихон"), reh("Хор", "девушки", time(15), time(16))])
        self.assertEqual(set(sent), GIRLS)

    def test_unknown_word_change_not_sent(self):
        sent = self.run_twice([reh("Клоп", "концертмейстер")],
                              [reh("Клоп", "концертмейстер", time(14), time(15))])
        self.assertEqual(sent, {})


if __name__ == "__main__":
    unittest.main()
