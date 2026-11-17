"""Профиль бота: логотип, тексты, setup_profile."""

import contextlib
import io
import math
import unittest

from PIL import Image

import bot as bot_module
import render_logo as rl
from bot import (COMMANDS, HELP_TEXT, PROFILE_DESCRIPTION, PROFILE_LIMITS, PROFILE_NAME,
                 PROFILE_SHORT_DESCRIPTION, TELEGRAM_COMMANDS, TelegramError, profile_calls,
                 setup_profile, tg_len)


class LogoTest(unittest.TestCase):
    def setUp(self):
        self.img = Image.open(io.BytesIO(rl.render_logo())).convert("RGB")

    def px(self, x, y):
        return "#%02x%02x%02x" % self.img.getpixel((x, y))

    def test_size_and_committed(self):
        self.assertEqual(self.img.size, (1024, 1024))
        self.assertTrue(rl.LOGO_FILE.exists(), "assets/bot_logo.png не закоммичен")
        committed = Image.open(rl.LOGO_FILE).convert("RGB")
        self.assertEqual(list(committed.getdata()), list(self.img.getdata()))

    def test_layout(self):
        g = 232
        self.assertEqual(self.px(5, 5), rl.BG)
        self.assertEqual(self.px(g + 130, g + 2), rl.GRID_LINE)            # линия y 0
        self.assertEqual(self.px(g + 280, g + 140), rl.GRID_LINE)          # линия y 138 (промежуток)
        self.assertEqual(self.px(g + 130, g + 70), rl.DARK_BLOCK)          # левая 24-114
        self.assertEqual(self.px(g + 130, g + 270), rl.ACCENT)             # левая 162-390
        self.assertEqual(self.px(g + 130, g + 480), rl.DARK_BLOCK)         # левая 438-528
        self.assertEqual(self.px(g + 430, g + 140), rl.DARK_BLOCK)         # правая 24-252
        self.assertEqual(self.px(g + 430, g + 345), rl.DARK_BLOCK)         # правая 300-390
        self.assertEqual(self.px(g + 430, g + 480), rl.ACCENT)             # правая 438-528
        self.assertEqual(self.px(g + 280, g + 70), rl.BG)                  # между колонками
        self.assertEqual(self.px(g + 280, g + 555), rl.GRID_LINE)          # последняя линия
        self.assertEqual(self.px(g + 280, g + 561), rl.BG)                 # группа кончается на 560

    def test_blocks_inside_circle(self):
        c = rl.SIZE / 2
        far = max(math.dist(p, (c, c)) for p in rl.block_corners())
        self.assertLessEqual(far, rl.SAFE_RADIUS)
        # вне круга 380 нет ни одного пикселя блоков (только фон или линии)
        for x in range(0, 1024, 8):
            for y in range(0, 1024, 8):
                if math.dist((x, y), (c, c)) > rl.SAFE_RADIUS:
                    self.assertIn(self.px(x, y), (rl.BG, rl.GRID_LINE), (x, y))


class ProfileTextsTest(unittest.TestCase):
    def test_lengths(self):
        self.assertLessEqual(tg_len(PROFILE_NAME), 64)
        self.assertLessEqual(tg_len(PROFILE_SHORT_DESCRIPTION), 120)
        self.assertLessEqual(tg_len(PROFILE_DESCRIPTION), 512)
        self.assertEqual(PROFILE_LIMITS, {"name": 64, "short_description": 120, "description": 512})

    def test_texts(self):
        self.assertEqual(PROFILE_NAME, "Расписание репетиций")
        self.assertEqual(PROFILE_SHORT_DESCRIPTION, "Присылает ваше расписание репетиций каждый "
                         "вечер и сообщает, если в таблице что-то поменялось.")
        self.assertTrue(PROFILE_DESCRIPTION.startswith("Расписание репетиций вашего курса.\n\n"))

    def test_commands_single_source(self):
        expected = ["start", "today", "tomorrow", "week", "date", "next", "now", "at", "who",
                    "room", "free", "find", "mute", "unmute", "stop", "help"]
        self.assertEqual([c["command"] for c in TELEGRAM_COMMANDS], expected)
        self.assertEqual([c for c, _, _ in COMMANDS], expected)
        for cmd, _, desc in COMMANDS:                     # /help и меню — из одного списка
            self.assertIn(f"/{cmd}", HELP_TEXT)
            self.assertIn(desc, HELP_TEXT)
        for c in TELEGRAM_COMMANDS:
            self.assertRegex(c["command"], r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= tg_len(c["description"]) <= 256)
        menu = dict(profile_calls())["setMyCommands"]["commands"]
        self.assertEqual(menu, TELEGRAM_COMMANDS)


class FakeAPI:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def call(self, method, **params):
        if method in self.fail:
            raise TelegramError(f"{method}: Too Many Requests: retry after 600", 429)
        self.calls.append((method, params))
        return True


def run(apply, api):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = setup_profile(apply=apply, api=api)
    return code, out.getvalue()


class SetupProfileTest(unittest.TestCase):
    def test_check_mode_calls_nothing(self):
        api = FakeAPI()
        code, out = run(False, api)
        self.assertEqual((code, api.calls), (0, []))
        for part in (PROFILE_NAME, PROFILE_SHORT_DESCRIPTION, "/who — ", "Режим проверки"):
            self.assertIn(part, out)

    def test_apply(self):
        api = FakeAPI()
        code, out = run(True, api)
        self.assertEqual(code, 0)
        self.assertEqual([m for m, _ in api.calls],
                         ["setMyName", "setMyShortDescription", "setMyDescription", "setMyCommands"])
        params = dict(api.calls)
        self.assertEqual(params["setMyName"], {"name": PROFILE_NAME})
        self.assertEqual(params["setMyDescription"], {"description": PROFILE_DESCRIPTION})

    def test_failure_does_not_stop_others(self):
        api = FakeAPI(fail={"setMyName"})
        code, out = run(True, api)
        self.assertEqual(code, 1)
        self.assertEqual(len(api.calls), 3)
        self.assertIn("setMyName: не удалось", out)

    def test_token_not_printed(self):
        class Boom:
            def post(self, url, **kw):
                import requests
                raise requests.ConnectionError(f"failed {url}")
        api = bot_module.TelegramAPI("123456:SECRET-token", session=Boom())
        code, out = run(True, api)
        self.assertEqual(code, 1)
        self.assertNotIn("SECRET", out)


if __name__ == "__main__":
    unittest.main()
