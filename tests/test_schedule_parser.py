import unittest
from datetime import date, time

from schedule_parser import (Participant, parse_csv, parse_day_header,
                             parse_participants, parse_time_range)


class TimeRangeTest(unittest.TestCase):
    def test_formats(self):
        cases = {
            "10-11": (time(10), time(11)),
            "14:30 - 15:30 ": (time(14, 30), time(15, 30)),
            "16:15-16-35": (time(16, 15), time(16, 35)),
            "15:30- 16:30": (time(15, 30), time(16, 30)),
            "19:00–20:00": (time(19), time(20)),
            "16-35": (time(16, 35), None),
            "10-11:30": (time(10), time(11, 30)),
            "9.30-10.15": (time(9, 30), time(10, 15)),
            "12": (time(12), None),
            "": (None, None),
            "   ": (None, None),
            "уточнить": (None, None),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_time_range(raw), expected)


class ParticipantsTest(unittest.TestCase):
    def test_spaces_and_notes(self):
        self.assertEqual(
            parse_participants("Соня , Дианна ,василиса"),
            [Participant("Соня"), Participant("Дианна"), Participant("василиса")])
        self.assertEqual(
            parse_participants("Софа, Тимофей, (Василиса?)")[-1],
            Participant("Василиса", "?"))
        self.assertEqual(
            parse_participants("Маша (опоздает, к 15), Василиса К."),
            [Participant("Маша", "опоздает, к 15"), Participant("Василиса К.")])
        self.assertEqual(parse_participants(" , ,"), [])


class DayHeaderTest(unittest.TestCase):
    def test_header(self):
        self.assertEqual(parse_day_header("понедельник 16.11.26"),
                         ("понедельник", date(2026, 11, 16)))
        self.assertEqual(parse_day_header("Среда"), ("Среда", None))


CSV = """\
понедельник 16.11.26,,,,,,вторник 17.11.26,,,,,
301,,,309,,,301,,,,,
время,название,участники,время,название,участники,время,название,участники,,,
10-11,Эрнест,"Вася О, Мира",,,,,,,,,
,,,16-35,Клоп,(Василиса?),,,,,,
14:00,,,,,,,,,,,
"""


class ParseCsvTest(unittest.TestCase):
    def test_parse(self):
        recs = parse_csv(CSV)
        self.assertEqual(len(recs), 2)  # строка с одним временем без названия пропущена
        a, b = recs
        self.assertEqual((a.date, a.room, a.title, a.start, a.end),
                         (date(2026, 11, 16), "301", "Эрнест", time(10), time(11)))
        self.assertEqual([p.name for p in a.participants], ["Вася О", "Мира"])
        self.assertEqual((b.room, b.start, b.participants),
                         ("309", time(16, 35), [Participant("Василиса", "?")]))


if __name__ == "__main__":
    unittest.main()
