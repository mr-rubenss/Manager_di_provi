import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from bot import Bot, Store, format_line
from names import ALIASES_FILE, ALL, FEMALE, MALE, PEOPLE_FILE, NameBook, group_of
from schedule_parser import Participant, Rehearsal, parse_participants

D = date(2026, 11, 12)


def reh(title, people, start=time(12), end=time(13), room="301", day=D):
    return Rehearsal(date=day, weekday="", start=start, end=end, time_raw="",
                     title=title, room=room, participants=parse_participants(people))


# В people.json сейчас нет людей с полом «?», поэтому для тестов добавляем двоих
# (Саша, Женя) поверх реальных aliases.json и people.json.
def book():
    aliases = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    people = json.loads(PEOPLE_FILE.read_text(encoding="utf-8"))
    return NameBook(aliases, people | {"Саша": "?", "Женя": "?"})


# подписчики: девушки, парни и пол «?»
SUBS = {"1": "Соня", "2": "Дианна", "3": "Тихон", "4": "Фёдор", "5": "Саша", "6": "Женя"}
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
    Bot(api, store, book(), lambda: table, lambda: datetime(2026, 11, 11, 21),
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
        b = book()
        for name in ("Саша", "Женя"):
            self.assertIsNone(b.gender(name))
            self.assertTrue(b.matches(name, reh("x", "весь курс")))
            self.assertFalse(b.matches(name, reh("x", "девушки")))
            self.assertFalse(b.matches(name, reh("x", "парни")))

    def test_real_people_genders(self):
        b = NameBook.load()
        expected = {"Даня": MALE, "Мира": FEMALE, "Дея": FEMALE,
                    "Вася К": FEMALE, "Вася О": FEMALE,       # через алиасы — Василисы
                    "Мирослава": FEMALE}
        for name, gender in expected.items():
            self.assertEqual(b.gender(name), gender, name)
        self.assertEqual(b.unknown_gender(), [])
        self.assertTrue(b.matches("Вася К", reh("x", "девушки")))
        self.assertFalse(b.matches("Даня", reh("x", "девушки")))
        self.assertTrue(b.matches("Даня", reh("x", "парни")))

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


class VasilisaTest(unittest.TestCase):
    def setUp(self):
        self.book = NameBook.load()

    def test_vasya_is_vasilisa(self):
        self.assertEqual(self.book.key("Вася К"), self.book.key("Василиса К."))
        self.assertEqual(self.book.key("вася о"), self.book.key("Василиса О"))
        self.assertEqual(self.book.key("Мирослава"), self.book.key("Мира"))
        self.assertTrue(self.book.matches("Василиса К.", reh("x", "вася к , Мира")))
        self.assertTrue(self.book.matches("Мира", reh("x", "Мирослава")))

    def test_k_and_o_never_merge(self):
        pairs = [("Василиса К.", "Василиса О"), ("Вася К", "Вася О"),
                 ("Вася К", "Василиса О"), ("Вася О", "Василиса К.")]
        for a, b in pairs:
            self.assertNotEqual(self.book.key(a), self.book.key(b), (a, b))
        self.assertFalse(self.book.matches("Василиса К.", reh("x", "Вася О")))
        self.assertFalse(self.book.matches("Василиса О", reh("x", "Василиса К.")))
        self.assertFalse(self.book.matches("Вася К", reh("x", "Василиса О")))

    def test_alias_merging_k_and_o_rejected(self):
        from names import AliasError
        for bad in ({"Василиса К.": "Василиса О"}, {"Вася К": "Василиса О"},
                    {"Вася О": "Василиса К."}, {"Вася К": "Вася О"}):
            with self.assertRaises(AliasError):
                NameBook(bad)

    def test_registration_shows_main_names_once(self):
        names = self.book.registration_names(
            [reh("x", "Вася К, Василиса К., вася о, Василиса О, Мирослава, Мира")])
        self.assertEqual(names, ["Василиса К.", "Василиса О", "Мира"])

    def test_bare_names_are_ambiguous(self):
        table = [reh("x", "Василиса, Вася, Василиса К.")]
        found = {(m.name, m.candidates) for m in self.book.ambiguous(table)}
        self.assertEqual(found, {("Василиса", ("Василиса К.", "Василиса О")),
                                 ("Вася", ("Василиса К.", "Василиса О"))})
        self.assertEqual(self.book.unknown(table), [])          # не «непонятные»
        self.assertFalse(self.book.matches("Василиса К.", reh("x", "Вася")))
        self.assertFalse(self.book.matches("Василиса О", reh("x", "Василиса")))


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
                     ("Софа", "Лиза"), ("Ди", "Дея"), ("Василиса К.", "Василиса О")]:
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
            Bot(api, store, book(), lambda t=table: t,
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
