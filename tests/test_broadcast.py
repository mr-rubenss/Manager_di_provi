"""Объявление об обновлении: баннер и рассылка broadcast."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

import render_banner as rb
from bot import BROADCAST_FILE, Store, TelegramError, broadcast, tg_len


class BannerTest(unittest.TestCase):
    def setUp(self):
        self.img = Image.open(io.BytesIO(rb.render_banner())).convert("RGB")

    def px(self, x, y):
        return "#%02x%02x%02x" % self.img.getpixel((x, y))

    def test_size_and_committed_file(self):
        self.assertEqual(self.img.size, (1080, 1080))
        self.assertTrue(rb.BANNER_FILE.exists(), "assets/update_banner.png не закоммичен")
        committed = Image.open(rb.BANNER_FILE).convert("RGB")
        self.assertEqual(committed.size, (1080, 1080))
        self.assertEqual(committed.size, self.img.size)
        delta = ImageChops.difference(committed, self.img)
        # FreeType builds can rasterize the same layout slightly differently.
        changed = sum(pixel != (0, 0, 0) for pixel in delta.getdata())
        self.assertLess(changed / (committed.width * committed.height), 0.04)

    def test_layout_colors(self):
        gx, gy = 660, 88
        self.assertEqual(self.px(10, 10), rb.BG)
        self.assertEqual(self.px(gx + 80, gy + 30), rb.DARK_BLOCK)           # левая колонка 8-56
        self.assertEqual(self.px(gx + 80, gy + 120), rb.ACCENT)              # 72-184
        self.assertEqual(self.px(gx + 80, gy + 220), rb.DARK_BLOCK)          # 200-248
        self.assertEqual(self.px(gx + 260, gy + 80), rb.DARK_BLOCK)          # правая 40-120
        self.assertEqual(self.px(gx + 260, gy + 220), rb.ACCENT)             # 200-248
        self.assertEqual(self.px(gx + 80, gy + 64), rb.GRID_LINE)            # линия между блоками
        self.assertEqual(self.px(gx + 170, gy + 128), rb.GRID_LINE)          # линия в промежутке
        self.assertEqual(self.px(gx + 170, gy + 100), rb.BG)                 # между колонками
        self.assertEqual(self.px(81, 411), rb.ACCENT)                        # рамка /now
        self.assertEqual(self.px(81, 411 + 2 * rb.ROW_STEP), rb.ACCENT)      # рамка /room
        self.assertEqual(self.px(rb.MARGIN + 5, 1080 - rb.MARGIN - 5), rb.ACCENT)   # плашка
        self.assertEqual(self.px(rb.MARGIN + 5, 1080 - rb.MARGIN + 5), rb.BG)       # отступ 80

    def test_upd_and_shadow(self):
        # тень правее и ниже основного «UPD»
        amber = [(x, y) for x in range(80, 640, 4) for y in range(150, 360, 4)
                 if self.px(x, y) == rb.ACCENT]
        shadow = [(x, y) for x in range(80, 650, 4) for y in range(150, 370, 4)
                  if self.px(x, y) == rb.SHADOW]
        self.assertTrue(amber and shadow)
        self.assertGreater(max(x for x, _ in shadow), max(x for x, _ in amber))
        self.assertGreater(max(y for _, y in shadow), max(y for _, y in amber))
        self.assertGreaterEqual(min(y for _, y in amber), 160)               # верх ≈ 165

    def test_pixel_font_has_cyrillic(self):
        f = rb.font(rb.FONT_PIXEL, 40)
        notdef = bytes(f.getmask(""))
        for ch in "UPDЖЯё":
            self.assertNotEqual(bytes(f.getmask(ch)), notdef, ch)


class FakeAPI:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail or {}

    def send_photo(self, chat_id, png, filename="schedule.png", caption=None):
        if chat_id in self.fail:
            raise TelegramError("Forbidden: bot was blocked by the user", self.fail[chat_id])
        self.sent.append((chat_id, png, filename, caption))


def store_with(subs, muted=()):
    tmp = Path(tempfile.mkdtemp())
    st = Store(tmp / "state.json", tmp / "subs.json")
    st.subscribers = dict(subs)
    st.muted = set(muted)
    st.save()
    return st


def run(store, send, force=False, api=None, text=None):
    text_file = BROADCAST_FILE
    if text is not None:
        text_file = Path(tempfile.mkdtemp()) / "msg.txt"
        text_file.write_text(text, encoding="utf-8")
    sleeps, out = [], io.StringIO()
    with contextlib.redirect_stdout(out):
        code = broadcast(send=send, force=force, api=api, store=store, text_file=text_file,
                         banner=b"\x89PNG-banner", sleep=sleeps.append)
    return code, out.getvalue(), sleeps


SUBS = {"111111111": "Соня", "222222222": "Софа", "333333333": "Тихон"}


class BroadcastTest(unittest.TestCase):
    def test_message_file_fits_caption(self):
        text = BROADCAST_FILE.read_text(encoding="utf-8").strip()
        self.assertTrue(text.startswith("Обновление бота"))
        self.assertLessEqual(tg_len(text), 1024)
        self.assertLessEqual(len(text), 1024)
        self.assertNotIn("<b>", text)                                        # обычный текст

    def test_check_mode_sends_nothing(self):
        api = FakeAPI()
        code, out, _ = run(store_with(SUBS), send=False, api=api)
        self.assertEqual(code, 0)
        self.assertEqual(api.sent, [])
        self.assertIn("Длина: ", out)
        self.assertIn("Получателей: 3", out)
        self.assertIn("ничего не отправлено", out)

    def test_send_to_everyone_including_muted(self):
        st = store_with(SUBS, muted={"222222222"})
        api = FakeAPI()
        code, out, sleeps = run(st, send=True, api=api)
        text = BROADCAST_FILE.read_text(encoding="utf-8").strip()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(c for c, *_ in api.sent), [111111111, 222222222, 333333333])
        for _, png, filename, caption in api.sent:                          # одно сообщение
            self.assertEqual(png, b"\x89PNG-banner")
            self.assertEqual(caption, text)
        self.assertEqual(sleeps, [0.1, 0.1])                                  # пауза между
        self.assertIn("Объявление: отправлено 3, не удалось 0", out)

    def test_repeat_blocked_and_force(self):
        st = store_with(SUBS)
        run(st, send=True, api=FakeAPI())
        api = FakeAPI()
        _, out, _ = run(st, send=True, api=api)
        self.assertEqual(api.sent, [])
        self.assertIn("Повтор не отправляю", out)
        run(st, send=True, force=True, api=api)
        self.assertEqual(len(api.sent), 3)
        self.assertEqual(len(st.state["broadcast_hashes"]), 1)               # хэш один
        # хэш сохранён в файле state.json
        self.assertEqual(Store(st.state_file, st.subscribers_file).state["broadcast_hashes"],
                         st.state["broadcast_hashes"])

    def test_other_text_is_not_a_repeat(self):
        st = store_with(SUBS)
        run(st, send=True, api=FakeAPI(), text="Первое объявление")
        api = FakeAPI()
        run(st, send=True, api=api, text="Второе объявление")
        self.assertEqual(len(api.sent), 3)

    def test_blocked_removed_and_counts_only_in_log(self):
        st = store_with(SUBS)
        api = FakeAPI(fail={222222222: 403, 333333333: 400})
        _, out, _ = run(st, send=True, api=api)
        self.assertIn("Объявление: отправлено 1, не удалось 2", out)
        self.assertNotIn("222222222", st.subscribers)                        # заблокировал
        self.assertIn("333333333", st.subscribers)                           # другая ошибка
        log_part = out.split("----------------------------")[-1]
        for chat in SUBS:
            self.assertNotIn(chat, log_part)                                 # без chat_id

    def test_nothing_sent_means_no_hash(self):
        st = store_with(SUBS)
        run(st, send=True, api=FakeAPI(fail={int(c): 500 for c in SUBS}))
        self.assertNotIn("broadcast_hashes", st.state)                       # можно повторить

    def test_too_long_or_empty_refused(self):
        api = FakeAPI()
        code, out, _ = run(store_with(SUBS), send=True, api=api, text="я" * 1025)
        self.assertEqual((code, api.sent), (2, []))
        code, _, _ = run(store_with(SUBS), send=True, api=api, text="   ")
        self.assertEqual((code, api.sent), (2, []))


if __name__ == "__main__":
    unittest.main()
