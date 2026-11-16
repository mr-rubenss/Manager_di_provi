"""Картинка-сетка: render_image.py и отправка картинок ботом."""

import io
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

from PIL import Image

import bot as bot_module
import render_image as ri
from bot import Bot, DryRunAPI, Store, TelegramError
from names import NameBook
from schedule_parser import Rehearsal, parse_participants

D = date(2026, 11, 17)
NOW = datetime(2026, 11, 16, 21, 5)


def reh(title, people, start, end, room="301", day=D):
    return Rehearsal(date=day, weekday="", start=start, end=end, time_raw="",
                     title=title, room=room, participants=parse_participants(people))


def render(items, person="Соня", day=D):
    png = ri.render_schedule_image(day, person, items, NOW)
    img = Image.open(io.BytesIO(png))
    img.load()
    return png, img


def amber_pixels(img):
    target = (0xF2, 0xB2, 0x4A)
    return sum(1 for px in img.convert("RGB").getdata() if px == target)


class FontsTest(unittest.TestCase):
    def test_fonts_present_and_cyrillic(self):
        for path in (ri.FONT_MEDIUM, ri.FONT_BOLD, ri.FONT_EXTRABOLD, ri.FONT_DATE):
            self.assertTrue(path.exists(), path)
            f = ri.font(path, 40)
            notdef = bytes(f.getmask(""))
            for ch in "ЁЖЩЯёжщя–…":
                self.assertNotEqual(bytes(f.getmask(ch)), notdef, (path.name, ch))
        self.assertEqual(ri.font(ri.FONT_EXTRABOLD, 20).getname(), ("Manrope", "ExtraBold"))
        self.assertEqual(ri.font(ri.FONT_DATE, 20).getname(), ("Playfair Display", "Bold"))


class RenderTest(unittest.TestCase):
    def check_png(self, png, img):
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.width, ri.WIDTH)
        self.assertGreater(img.height, 400)

    def test_no_rehearsals(self):
        png, img = render([])
        self.check_png(png, img)
        self.assertLess(img.height, 900)

    def test_one(self):
        png, img = render([reh("Альба", "Соня", time(16), time(17))])
        self.check_png(png, img)
        # шапка + легенда + колонки + 1 час сетки (90 px) + низ
        self.assertTrue(500 < img.height < 800, img.height)

    def test_many_rehearsals_and_scale(self):
        items = [reh(f"Отрывок {h}", "Соня, Ева", time(h), time(h, 50), "301" if h % 2 else "309")
                 for h in range(8, 23)]                       # 15 часов
        png, img = render(items)
        self.check_png(png, img)
        hours = 15
        self.assertEqual(ri.px_per_hour(hours), ri.MAX_GRID_H / hours)   # ужато до 1000 px
        self.assertLess(img.height, 1000 + 700)

    def test_scale_limits(self):
        self.assertEqual(ri.px_per_hour(5), 90)
        self.assertEqual(ri.px_per_hour(11), 90)               # 990 px — ещё помещается
        self.assertAlmostEqual(ri.px_per_hour(12), 1000 / 12)
        self.assertEqual(ri.px_per_hour(24), 60)               # не меньше 60 px на час

    def test_hour_range_rounds_to_whole_hours(self):
        items = [reh("a", "Ева", time(13, 50), time(14, 50)), reh("b", "Ева", time(20), time(21, 30))]
        self.assertEqual(ri.hour_range(items), (13, 22))

    def test_parallel_in_one_room(self):
        a = reh("110 в тени", "Миша П", time(17), time(18), "309")
        b = reh("солдатик", "Соня", time(17), time(18), "309")
        c = reh("Триумф", "Ева", time(17, 30), time(18, 20), "309")
        d = reh("Поздно", "Ева", time(19), time(20), "309")
        lanes = {r.title: (lane, n) for r, lane, n in ri.assign_lanes([a, b, c, d])}
        self.assertEqual(lanes["110 в тени"], (0, 3))
        self.assertEqual(lanes["солдатик"], (1, 3))
        self.assertEqual(lanes["Триумф"], (2, 3))
        self.assertEqual(lanes["Поздно"], (0, 1))              # не пересекается — на всю ширину
        # встык — не параллельно
        e = reh("Ранний", "Ева", time(16), time(17), "309")
        self.assertEqual({r.title: n for r, _, n in ri.assign_lanes([e, d])}, {"Ранний": 1, "Поздно": 1})
        self.check_png(*render([a, b, c, d]))

    def test_long_titles_and_people(self):
        long_title = "Очень длинное название отрывка, которое никак не помещается в одну строку"
        many = ", ".join(f"Участник{i}" for i in range(30))
        items = [reh(long_title, "Соня, " + many, time(10), time(12)),
                 reh(long_title, many, time(10), time(10, 20), "309"),        # короткий блок
                 reh(long_title, many, time(10), time(11), "309")]            # параллельный
        self.check_png(*render(items))

    def test_fit_text_ellipsis_and_min_size(self):
        f, t = ri.fit_text("Короткое", ri.FONT_BOLD, 26, 500)
        self.assertEqual((f.size, t), (26, "Короткое"))
        f, t = ri.fit_text("Очень длинное название отрывка " * 3, ri.FONT_BOLD, 26, 200)
        self.assertEqual(f.size, ri.SIZE_MIN)                 # не меньше 18
        self.assertTrue(t.endswith("…"))
        self.assertLessEqual(ri.text_w(t, f), 200)
        self.assertEqual(ri.fit_text("Текст", ri.FONT_BOLD, 26, 5)[1], "")

    def test_three_and_more_rooms(self):
        items = [reh("a", "Соня", time(10), time(11), "309"),
                 reh("b", "Ева", time(10), time(11), "301"),
                 reh("c", "Ева", time(11), time(12), "1001"),
                 reh("d", "Ева", time(12), time(13), "45"),
                 reh("e", "Ева", time(12), time(13), "Зал")]
        self.assertEqual(ri.day_rooms(items), ["45", "301", "309", "1001", "Зал"])
        self.check_png(*render(items))

    def test_only_this_day_and_untimed(self):
        items = [reh("Сегодня", "Соня", time(10), time(11)),
                 reh("Другой день", "Соня", time(10), time(11), day=date(2026, 11, 18)),
                 reh("Без конца", "Соня", time(12), None),
                 reh("Без времени", "Соня", None, None)]
        self.assertEqual(ri.day_rooms([r for r in items if r.date == D]), ["301"])
        self.check_png(*render(items))

    def test_mine_highlighted(self):
        items = [reh("Альба", "Соня", time(10), time(11)),
                 reh("Карусель", "весь курс", time(11), time(12)),
                 reh("Хор", "хор (женщины)", time(12), time(13)),
                 reh("Чужая", "Тихон", time(13), time(14))]
        sonya = amber_pixels(render(items, "Соня")[1])            # 3 янтарных блока
        tikhon = amber_pixels(render(items, "Тихон")[1])          # 2: своя + «весь курс»
        nobody = amber_pixels(render([reh("Чужая", "Тихон", time(13), time(14))], "Соня")[1])
        self.assertGreater(sonya, tikhon)
        self.assertGreater(tikhon, nobody)

    def test_generated_at_aware(self):
        from datetime import timedelta, timezone
        utc = datetime(2026, 11, 16, 18, 5, tzinfo=timezone.utc)
        self.assertEqual(ri._moscow(utc).strftime("%H:%M"), "21:05")
        self.assertEqual(ri._moscow(datetime(2026, 11, 16, 21, 5)).strftime("%H:%M"), "21:05")
        png = ri.render_schedule_image(D, "Соня", [reh("a", "Соня", time(1), time(2))],
                                       datetime(2026, 11, 16, 21, 5, tzinfo=timezone(timedelta(hours=3))))
        self.assertTrue(png.startswith(b"\x89PNG"))


# ------------------------------------------------------------------------- #
# Отправка ботом
# ------------------------------------------------------------------------- #

SCHEDULE = [reh("Альба", "Соня", time(16), time(17)), reh("Эрнест", "Тихон", time(15), time(16), "309"),
            reh("Сегодня", "Соня", time(12), time(13), day=date(2026, 11, 16))]


class PhotoAPI:
    def __init__(self, fail_photo=False):
        self.events, self.updates, self.fail_photo = [], [], fail_photo

    def call(self, method, **params):
        if method == "getUpdates":
            res, self.updates = self.updates, []
            return res
        return True

    def send_photo(self, chat_id, png, filename="schedule.png", caption=None):
        if self.fail_photo:
            raise TelegramError("sendPhoto: Bad Request",
                                403 if self.fail_photo == 403 else 400)
        self.events.append(("photo", chat_id, png[:8], filename, caption))

    def send_message(self, chat_id, text, keyboard=None):
        self.events.append(("text", chat_id, text))


def make(now=NOW, fail_photo=False, subs=None):
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "s.json", tmp / "subs.json")
    store.subscribers = dict(subs or {"1": "Соня", "2": "Тихон", "3": "Арсений"})
    api = PhotoAPI(fail_photo)
    return Bot(api, store, NameBook.load(), lambda: SCHEDULE, lambda: now,
               snapshot_file=tmp / "snap.json"), api


def msg(uid, text, chat=1):
    return {"update_id": uid, "message": {"chat": {"id": chat, "type": "private"}, "text": text}}


class SendPhotoTest(unittest.TestCase):
    def test_daily_one_message_with_caption(self):
        b, api = make()
        b.send_daily()
        self.assertEqual([(e[0], e[1]) for e in api.events], [("photo", 1), ("photo", 2)])
        kind, chat, head, filename, caption = api.events[0]
        self.assertEqual(head, b"\x89PNG\r\n\x1a\n")
        self.assertEqual(filename, "schedule-2026-11-17.png")
        self.assertEqual(caption, "РАСПИСАНИЕ РЕПЕТИЦИЙ 17.11.26:\n"
                                  "16:00-17:00 Альба (задействованы: Соня; аудитория 301)")
        self.assertNotIn(3, [e[1] for e in api.events])        # у Арсения пусто — ничего

    def test_long_text_photo_then_full_text(self):
        many = [reh(f"Отрывок с длинным названием номер {i}", "Соня, Ева, Тихон, Миша К, Миша П",
                    time(9 + i // 2, 30 * (i % 2)), time(9 + i // 2, 30 * (i % 2) + 29))
                for i in range(20)]
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "s.json", tmp / "subs.json")
        store.subscribers = {"1": "Соня"}
        api = PhotoAPI()
        b = Bot(api, store, NameBook.load(), lambda: many, lambda: NOW, snapshot_file=tmp / "x.json")
        b.send_daily()
        self.assertEqual([e[0] for e in api.events], ["photo", "text"])
        self.assertIsNone(api.events[0][4])                       # картинка без подписи
        text = api.events[1][2]
        self.assertGreater(bot_module.tg_len(text), bot_module.CAPTION_LIMIT)
        self.assertEqual(text.count("Отрывок с длинным названием"), 20)   # полный, не обрезан
        self.assertFalse(text.endswith("…"))

    def test_caption_limit_boundary(self):
        self.assertEqual(bot_module.tg_len("🎭"), 2)
        b, api = make()
        exact = "я" * bot_module.CAPTION_LIMIT
        b._deliver_day(1, "Соня", D, exact)
        self.assertEqual([(e[0], e[4]) for e in api.events], [("photo", exact)])
        api.events.clear()
        over = "я" * (bot_module.CAPTION_LIMIT - 1) + "🎭"           # 1001 по счёту Telegram
        b._deliver_day(1, "Соня", D, over)
        self.assertEqual([e[0] for e in api.events], ["photo", "text"])
        self.assertIsNone(api.events[0][4])
        self.assertEqual(api.events[1][2], over)

    def test_photo_failure_still_sends_text(self):
        b, api = make(fail_photo=True)
        self.assertEqual(b.send_daily(), 2)
        self.assertEqual([e[0] for e in api.events], ["text", "text"])
        self.assertIn("16:00-17:00 Альба", api.events[0][2])

    def test_blocked_user_removed_without_text_attempt(self):
        b, api = make(fail_photo=403)
        self.assertEqual(b.send_daily(), 0)
        self.assertEqual(api.events, [])
        self.assertNotIn("1", b.store.subscribers)

    def test_render_failure_still_sends_text(self):
        b, api = make()
        with mock.patch("render_image.render_schedule_image", side_effect=RuntimeError("boom")):
            b.send_daily()
        self.assertEqual([e[0] for e in api.events], ["text", "text"])

    def test_commands(self):
        b, api = make(now=datetime(2026, 11, 16, 10, 0))
        cases = {"/today": ["photo"], "/tomorrow": ["photo"], "/date 17.11": ["photo"],
                 "/week": ["text"], "/date 25.11": ["text"]}   # пусто — без картинки
        for i, (cmd, expected) in enumerate(cases.items(), start=1):
            with self.subTest(cmd=cmd):
                api.events.clear()
                api.updates = [msg(i, cmd)]
                b.poll()
                self.assertEqual([e[0] for e in api.events], expected)
                if expected == ["photo"]:
                    self.assertTrue(api.events[0][4].startswith("РАСПИСАНИЕ РЕПЕТИЦИЙ"))

    def test_commands_photo_failure_text_fallback(self):
        b, api = make(now=datetime(2026, 11, 16, 10, 0), fail_photo=True)
        api.updates = [msg(1, "/tomorrow")]
        b.poll()
        self.assertEqual([e[0] for e in api.events], ["text"])

    def test_no_rehearsals_no_photo(self):
        b, api = make(now=datetime(2026, 11, 16, 10, 0), subs={"3": "Арсений"})
        api.updates = [msg(1, "/today", chat=3), msg(2, "/tomorrow", chat=3)]
        b.poll()
        self.assertEqual([e[0] for e in api.events], ["text", "text"])

    def test_dry_run_writes_png_to_out(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(bot_module, "OUT_DIR", tmp):
            with mock.patch("builtins.print"):
                DryRunAPI().send_photo(5, b"\x89PNG-test", filename="schedule-2026-11-17.png")
        self.assertEqual((tmp / "dry-run-5-schedule-2026-11-17.png").read_bytes(), b"\x89PNG-test")

    def test_send_photo_multipart_and_token_redacted(self):
        captured = {}

        class Session:
            def post(self, url, **kw):
                captured.update(url=url, **kw)

                class R:
                    def json(self_inner):
                        return {"ok": True, "result": {}}
                return R()

        api = bot_module.TelegramAPI("123:SECRET", session=Session())
        api.send_photo(7, b"\x89PNG", filename="x.png")
        self.assertTrue(captured["url"].endswith("/sendPhoto"))
        self.assertEqual(captured["data"], {"chat_id": 7})
        api.send_photo(7, b"\x89PNG", filename="x.png", caption="Текст <b>как есть</b>")
        self.assertEqual(captured["data"], {"chat_id": 7, "caption": "Текст <b>как есть</b>"})
        self.assertNotIn("parse_mode", captured["data"])            # обычный текст
        self.assertEqual(captured["files"]["photo"], ("x.png", b"\x89PNG", "image/png"))

        class Broken:
            def post(self, url, **kw):
                import requests
                raise requests.ConnectionError(f"failed {url}")
        with self.assertRaises(TelegramError) as ctx:
            bot_module.TelegramAPI("123:SECRET", session=Broken()).send_photo(7, b"x")
        self.assertNotIn("SECRET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
