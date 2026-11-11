"""Тесты парсера на «кривых» данных из таблицы."""

import unittest
from datetime import date, time

from schedule_parser import (Participant, parse_csv, parse_day_header,
                             parse_participants, parse_time_range)

P = Participant


class TimeRangeTest(unittest.TestCase):
    def check(self, cases):
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_time_range(raw), expected)

    def test_hours_only(self):
        self.check({
            "10-11": (time(10), time(11)),
            "9-10": (time(9), time(10)),
            "10 - 11": (time(10), time(11)),
            "10ч-11ч": (time(10), time(11)),
            "с 10 до 11": (time(10), time(11)),
            "12": (time(12), None),
        })

    def test_hours_and_minutes(self):
        self.check({
            "10:00-11:00": (time(10), time(11)),
            "09:05-09:50": (time(9, 5), time(9, 50)),
            "17:50-18:40 ": (time(17, 50), time(18, 40)),
            "14:30 - 15:30 ": (time(14, 30), time(15, 30)),
            "15:30- 16:30": (time(15, 30), time(16, 30)),
            "10:00 до 11:00": (time(10), time(11)),
            "10:00-11:00 (пер.)": (time(10), time(11)),
        })

    def test_other_separators(self):
        self.check({
            "9.30-10.15": (time(9, 30), time(10, 15)),
            "10,30-11,30": (time(10, 30), time(11, 30)),
            "1030-1130": (time(10, 30), time(11, 30)),
        })

    def test_dashes(self):
        self.check({
            "19:00–20:00": (time(19), time(20)),     # en dash
            "10:00 — 11:00": (time(10), time(11)),   # em dash
            "10−11": (time(10), time(11)),           # минус
            "10‐11": (time(10), time(11)),           # дефис U+2010
        })

    def test_mixed_precision(self):
        self.check({
            "10:30-11": (time(10, 30), time(11)),
            "10-11:30": (time(10), time(11, 30)),
        })

    def test_typos_with_dash_instead_of_colon(self):
        self.check({
            "16:15-16-35": (time(16, 15), time(16, 35)),
            "16-35": (time(16, 35), None),           # 35 часов не бывает -> 16:35
            "16:35": (time(16, 35), None),
        })

    def test_whitespace_garbage(self):
        self.check({
            "\xa010-11\xa0": (time(10), time(11)),
            "  10 :00 - 11: 00  ": (time(10), time(11)),
            "\n10-11\n": (time(10), time(11)),
        })

    def test_open_end(self):
        self.check({"10:00-": (time(10), None)})

    def test_unclear_is_not_guessed(self):
        # Всё это бот не угадывает: в сообщении будет текст ячейки как есть.
        self.check({
            "": (None, None),
            "   ": (None, None),
            "?": (None, None),
            "уточнить": (None, None),
            "весь день": (None, None),
            "-11:00": (None, None),               # нет начала
            "25-26": (None, None),                # нет таких часов
            "10:60-11:00": (None, None),          # нет таких минут
            "11-10": (None, None),                # конец раньше начала
            "14:30-13:30": (None, None),
            "16-16": (None, None),
            "23-1": (None, None),                 # через полночь — не угадываем
            "12:00-13:00, 14-15": (None, None),   # два интервала в одной ячейке
        })


class ParticipantsTest(unittest.TestCase):
    def check(self, cases):
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_participants(raw), expected)

    def test_spacing(self):
        self.check({
            "Соня , Дианна ,василиса": [P("Соня"), P("Дианна"), P("василиса")],
            "Вася О , тима,  миша п": [P("Вася О"), P("тима"), P("миша п")],
            "Софа,Даня,Миша К": [P("Софа"), P("Даня"), P("Миша К")],
            "  Ева  ": [P("Ева")],
            "Ева\xa0, Мира": [P("Ева"), P("Мира")],
        })

    def test_empty_items(self):
        self.check({
            "": [],
            "  ": [],
            " , ,": [],
            "Ева,,Миша К,": [P("Ева"), P("Миша К")],
        })

    def test_other_separators(self):
        self.check({
            "Ева; Миша К": [P("Ева"), P("Миша К")],
            "Ди\nМира": [P("Ди"), P("Мира")],                  # перенос строки в ячейке
            "Ди\r\n\r\nМира; Ева": [P("Ди"), P("Мира"), P("Ева")],
            "Соня и Софа": [P("Соня"), P("Софа")],
            "Ираида, Инна": [P("Ираида"), P("Инна")],          # «и» внутри имени не режем
        })

    def test_notes_in_brackets(self):
        self.check({
            "(Василиса?)": [P("Василиса", "?")],
            "Софа, Тимофей, (Василиса?)": [P("Софа"), P("Тимофей"), P("Василиса", "?")],
            "(Василиса К?)": [P("Василиса К", "?")],
            "Тихон (?)": [P("Тихон", "?")],
            "Соня[?]": [P("Соня", "?")],
            "Мира(с 15:00)": [P("Мира", "с 15:00")],
            "Мира (без костюма), Даня (?)": [P("Мира", "без костюма"), P("Даня", "?")],
            "Дианна (Ди)": [P("Дианна", "Ди")],
        })

    def test_separators_inside_brackets_kept(self):
        self.check({
            "Маша (опоздает, к 15), Василиса К.": [P("Маша", "опоздает, к 15"), P("Василиса К.")],
            "Маша (опоздает и уйдёт), Соня": [P("Маша", "опоздает и уйдёт"), P("Соня")],
            "Мира (с 15:00;\nбез костюма)": [P("Мира", "с 15:00; без костюма")],
        })

    def test_broken_brackets(self):
        self.check({
            "Маша (": [P("Маша")],
            "Ева)": [P("Ева")],
        })

    def test_initials_and_groups_kept_as_written(self):
        self.check({
            "Василиса К., Миша К.": [P("Василиса К."), P("Миша К.")],
            "весь курс, хор": [P("весь курс"), P("хор")],
            "хор (женщины)": [P("хор", "женщины")],
            "(все)": [P("все")],
        })


class DayHeaderTest(unittest.TestCase):
    def test_headers(self):
        cases = {
            "понедельник 16.11.26": ("понедельник", date(2026, 11, 16)),
            "ВТОРНИК 17.11.2026": ("вторник", date(2026, 11, 17)),
            "среда, 11.11.26": ("среда", date(2026, 11, 11)),
            "четверг 1.1.27": ("четверг", date(2027, 1, 1)),
            " воскресенье  15/11/26 ": ("воскресенье", date(2026, 11, 15)),
            "14.11.26": ("", date(2026, 11, 14)),
            "пт 31.02.26": ("пт", None),          # нет такой даты
            "Среда": ("Среда", None),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_day_header(raw), expected)


# Два дня, по две аудитории; рваные строки, пустые строки посреди данных,
# NBSP, перенос строки в ячейке, лишний столбец справа.
MESSY_CSV = (
    "понедельник 16.11.26,,,,,,вторник 17.11.26,,,,,\n"
    "301,,,309,,,301,,,,,\n"
    "время,название,участники,время ,название ,участники,время,название,участники,,,\n"
    "10-11,Эрнест,\"Вася О, Мира\",16:15-16-35,Клоп,\"Софа, (Василиса?)\",,,,,,\n"
    ",,,,,,\n"                                            # пустая и короткая строка
    "14:00,,,,,,,,,,,,лишнее\n"                           # одинокое время — не запись
    ",Без времени,Ева,,,,уточнить,Альба,\"Ди\nМира\",,,\n"
    "\xa012:00 - 13:00\xa0,  Хелоу   Долли ,Соня  и  Софа,,,,,,,,,\n"
    "11-10,Кривое время,Тихон,,,,,,,,,\n"
)


class ParseCsvTest(unittest.TestCase):
    def setUp(self):
        self.recs = parse_csv(MESSY_CSV)
        self.by_title = {r.title: r for r in self.recs}

    def test_count_and_skips(self):
        self.assertEqual(sorted(self.by_title),
                         ["Альба", "Без времени", "Клоп", "Кривое время",
                          "Хелоу Долли", "Эрнест"])

    def test_days_and_rooms(self):
        self.assertEqual((self.by_title["Эрнест"].date, self.by_title["Эрнест"].room),
                         (date(2026, 11, 16), "301"))
        self.assertEqual(self.by_title["Клоп"].room, "309")
        self.assertEqual((self.by_title["Альба"].date, self.by_title["Альба"].room),
                         (date(2026, 11, 17), "301"))
        self.assertEqual(self.by_title["Альба"].weekday, "вторник")

    def test_time_fields(self):
        r = self.by_title["Клоп"]
        self.assertEqual((r.start, r.end), (time(16, 15), time(16, 35)))
        r = self.by_title["Хелоу Долли"]
        self.assertEqual((r.start, r.end), (time(12), time(13)))
        r = self.by_title["Без времени"]
        self.assertEqual((r.start, r.end, r.time_raw), (None, None, ""))
        r = self.by_title["Альба"]
        self.assertEqual((r.start, r.time_raw), (None, "уточнить"))
        r = self.by_title["Кривое время"]
        self.assertEqual((r.start, r.end, r.time_raw), (None, None, "11-10"))

    def test_participants(self):
        self.assertEqual(self.by_title["Эрнест"].participants, [P("Вася О"), P("Мира")])
        self.assertEqual(self.by_title["Клоп"].participants, [P("Софа"), P("Василиса", "?")])
        self.assertEqual(self.by_title["Альба"].participants, [P("Ди"), P("Мира")])
        self.assertEqual(self.by_title["Хелоу Долли"].participants, [P("Соня"), P("Софа")])

    def test_sorted_by_date_then_time(self):
        keys = [(r.date, r.start or time.max) for r in self.recs]
        self.assertEqual(keys, sorted(keys))

    def test_parallel_rehearsals_not_merged(self):
        csv = ("среда 11.11.26,,\n301,,\nвремя,название,участники\n"
               "18:20-19:00,солдатик драм,\"Арсений, Дианна\"\n"
               "18:20-19:00,драм Лилиом,\"Ева, Миша К\"\n"
               "18:20-19:00,драм Лилиом,\"Ева, Миша К\"\n")    # даже точный дубль
        recs = parse_csv(csv)
        self.assertEqual(len(recs), 3)
        self.assertEqual({(r.start, r.room) for r in recs}, {(time(18, 20), "301")})

    def test_degenerate_input(self):
        self.assertEqual(parse_csv(""), [])
        self.assertEqual(parse_csv("понедельник 16.11.26\n301\n"), [])
        self.assertEqual(parse_csv("понедельник 16.11.26,,\n301,,\nвремя,название,участники\n"), [])


if __name__ == "__main__":
    unittest.main()
