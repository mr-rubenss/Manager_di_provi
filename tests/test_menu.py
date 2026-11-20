import json
import unittest
from unittest.mock import patch

from bot import INPUT_TTL, Store, TelegramError, send_announcement
from tests.test_commands import Env


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.bot, self.store = self.env.bot, self.env.store
        self.chat = 7
        self.store.subscribers[str(self.chat)] = "Соня"

    def message(self, text, chat=None):
        if chat is None:
            chat = self.chat
        self.bot.handle_update({"message": {
            "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": text}})
        return self.env.api.sent[-1] if self.env.api.sent else None

    def callback(self, data, actor=None, chat=None):
        if chat is None:
            chat = self.chat
        if actor is None:
            actor = chat
        self.bot.handle_update({"callback_query": {"id": "clicked", "data": data,
            "from": {"id": actor}, "message": {"chat": {"id": chat, "type": "private"}}}})
        return self.env.api.sent[-1] if self.env.api.sent else None

    @staticmethod
    def button(response, prefix):
        return next(btn["callback_data"] for row in response[2] for btn in row
                    if btn["callback_data"].startswith(prefix))

    def test_menu_and_start(self):
        for command in ("/menu", "/start"):
            response = self.message(command)
            rows = response[2]
            labels = [b["text"] for row in rows for b in row]
            self.assertEqual(labels, ["Сегодня", "Завтра", "На неделю", "Ближайшая репетиция",
                                     "Сейчас идут", "Расписание человека", "Аудитории",
                                     "Свободные аудитории", "Найти постановку", "Настройки", "Помощь"])
            self.assertTrue(all(len(row) <= 2 for row in rows))

    def test_every_existing_menu_handler(self):
        for action, handler in (("today", "send_personal"), ("tomorrow", "send_personal"),
                                ("week", "send_personal"), ("next", "send_next"),
                                ("now", "send_at"), ("who", "ask_who"), ("room", "handle_room"),
                                ("free", "handle_free"), ("settings", "send_settings")):
            with self.subTest(action=action), patch.object(self.bot, handler) as reused:
                self.message("/menu")
                self.callback("m:" + action)
                reused.assert_called_once()
        self.assertIn("/menu", self.callback("m:help")[1])
        with patch.object(self.bot, "send_find") as find:
            self.callback("m:find")
            self.message("эрнест")
            find.assert_called_once_with(self.chat, "эрнест")

    def test_callback_is_acknowledged_and_result_has_back_button(self):
        with patch.object(self.bot.api, "call", wraps=self.bot.api.call) as api:
            response = self.callback("m:now")
            api.assert_any_call("answerCallbackQuery", callback_query_id="clicked")
        self.assertEqual(self.button(response, "m:menu"), "m:menu")

    def test_settings_and_independent_toggles_survive_restart(self):
        self.assertIn("Имя: Соня", self.message("/settings")[1])
        for kind in ("daily", "changes", "announcements"):
            response = self.callback("s:" + kind)
            self.assertIn("Уведомление выключено", response[1])
            store = Store(self.store.state_file, self.store.subscribers_file)
            self.assertFalse(store.enabled(self.chat, kind))
            expected = [k != kind for k in ("daily", "changes", "announcements")]
            self.assertEqual([store.enabled(self.chat, k) for k in
                              ("daily", "changes", "announcements")], expected)
            self.callback("s:" + kind)

    def test_legacy_preferences_and_commands(self):
        self.store.subscribers_file.write_text(json.dumps({
            "7": "Соня", "8": {"name": "Софа", "muted": True}}), encoding="utf-8")
        self.bot.store = self.store = Store(self.store.state_file, self.store.subscribers_file)
        self.assertFalse(self.store.enabled(8, "daily"))
        self.assertFalse(self.store.enabled(8, "changes"))
        self.assertTrue(self.store.enabled(8, "announcements"))
        self.callback("s:announcements")
        self.callback("s:daily")
        self.message("/mute")
        self.assertFalse(self.store.enabled(7, "changes"))
        self.message("/unmute")
        self.assertTrue(self.store.enabled(7, "daily"))
        self.assertTrue(self.store.enabled(7, "changes"))
        self.assertFalse(self.store.enabled(7, "announcements"))
        self.assertEqual(set(self.store.subscribers), {"7", "8"})

    def test_change_name_preserves_settings(self):
        self.callback("s:daily")
        self.callback("s:name")
        self.message("Софа")
        self.assertEqual(self.store.subscribers["7"], "Софа")
        self.assertFalse(self.store.enabled(7, "daily"))

    def test_deletion_confirmation_and_cancel(self):
        first = self.button(self.callback("s:delete"), "s:delete:")
        self.assertIn("7", self.store.subscribers)
        self.callback("s:menu")
        self.callback(first)
        self.assertIn("7", self.store.subscribers)
        second = self.button(self.callback("s:delete"), "s:delete:")
        self.callback(second)
        self.assertNotIn("7", self.store.subscribers)

    def test_cancel_and_expire_pending_input(self):
        with patch.object(self.bot, "send_find") as find:
            self.callback("m:find")
            self.message("/help")
            self.message("эрнест")
            self.callback("m:find")
            with patch.object(self.bot, "now", return_value=self.bot.now() + INPUT_TTL):
                self.message("эрнест")
            find.assert_not_called()
        self.assertNotIn("awaiting_input", self.store.state)

    def test_admin_allowlist_and_every_callback(self):
        self.bot.admins = {"7", "8"}
        self.assertIn("Управление", self.message("/admin")[1])
        self.assertEqual(self.message("/admin", chat=9)[1], "Команда недоступна")
        for action in ("menu", "status", "stats", "table", "changes", "changes:guess",
                       "announcement", "send:guess"):
            self.assertEqual(self.callback("a:" + action, chat=9)[1], "Команда недоступна")
            self.assertEqual(self.callback("a:" + action, actor=8)[1], "Команда недоступна")
        self.bot.admins.clear()
        self.assertEqual(self.message("/admin")[1], "Команда недоступна")

    def test_admin_stats_status_and_table_are_read_only(self):
        self.bot.admins = {"7"}
        self.store.subscribers["1234567890"] = "Софа"
        self.store.set_notice(7, "daily", False)
        stats = self.callback("a:stats")[1]
        self.assertIn("Подписчиков: 2", stats)
        self.assertIn("Расписание на завтра: 1", stats)
        self.assertNotIn("1234567890", stats)
        self.assertIn("Отчёт (МСК)", self.callback("a:status")[1])
        snapshot = self.bot.snapshot_file
        snapshot.write_text("unchanged", encoding="utf-8")
        self.assertIn("Таблица загружена", self.callback("a:table")[1])
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "unchanged")

    def test_admin_changes_are_confirmed_once(self):
        self.bot.admins = {"7"}
        with patch.object(self.bot, "check_changes", return_value=(3, 2)) as check:
            confirm = self.button(self.callback("a:changes"), "a:changes:")
            check.assert_not_called()
            self.assertIn("Изменений: 3. Уведомлено: 2", self.callback(confirm)[1])
            self.callback(confirm)
            check.assert_called_once_with(details=True)

    def test_announcement_preview_cancel_confirm_and_duplicate(self):
        self.bot.admins = {"7"}
        self.store.subscribers["8"] = "Софа"
        self.store.set_notice(8, "announcements", False)
        text = "Проверочное объявление"
        with patch.object(self.bot.api, "send_photo", create=True) as send:
            self.callback("a:announcement")
            preview = self.message(text)
            self.assertIn("Получателей: 1", preview[1])
            self.assertIn(text, preview[1])
            first = self.button(preview, "a:send:")
            send.assert_not_called()
            self.callback("a:menu")
            self.callback(first)
            send.assert_not_called()
            self.callback("a:announcement")
            second = self.button(self.message(text), "a:send:")
            self.assertIn("Отправлено: 1", self.callback(second)[1])
            self.callback(second)
            send.assert_called_once()
            self.callback("a:announcement")
            self.assertIn("уже рассылался", self.message(text)[1])
            self.assertNotIn(text, self.store.state_file.read_text(encoding="utf-8"))

    def test_announcement_photo_fallback_and_blocked_removal(self):
        self.store.subscribers["8"] = "Софа"
        with patch.object(self.bot.api, "send_photo", create=True, side_effect=[
                TelegramError("bad photo", 400), TelegramError("blocked", 403)]):
            result = send_announcement("Тест fallback", self.bot.api, self.store,
                                       banner=b"fake", sleep=lambda _: None)
        self.assertEqual(result, (1, 1, False))
        self.assertEqual(self.env.api.sent[-1][:2], (7, "Тест fallback"))
        self.assertNotIn("8", self.store.subscribers)


if __name__ == "__main__":
    unittest.main()
