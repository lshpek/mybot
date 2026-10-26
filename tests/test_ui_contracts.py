"""Keyboard regression tests, with small type stubs for offline environments."""
import importlib
import sys
import types
import unittest

try:
    import aiogram.types  # noqa: F401
except ModuleNotFoundError:
    package = types.ModuleType("aiogram")
    package.__path__ = []
    types_mod = types.ModuleType("aiogram.types")

    class InlineKeyboardButton:
        def __init__(self, *, text, callback_data=None, url=None, style=None):
            self.text, self.callback_data, self.url, self.style = text, callback_data, url, style

    class InlineKeyboardMarkup:
        def __init__(self, *, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    types_mod.InlineKeyboardButton = InlineKeyboardButton
    types_mod.InlineKeyboardMarkup = InlineKeyboardMarkup
    sys.modules.setdefault("aiogram", package)
    sys.modules.setdefault("aiogram.types", types_mod)

keyboards = importlib.import_module("app.keyboards")


def flatten(markup):
    return [button for row in markup.inline_keyboard for button in row]


class KeyboardContractTests(unittest.TestCase):
    def test_regular_notification_has_open_and_home_actions(self):
        buttons = flatten(keyboards.customer_notification_menu(12, 56))
        self.assertEqual(len(buttons), 2)
        callbacks = [button.callback_data for button in buttons]
        self.assertIn("customer:open:12:56", callbacks)
        self.assertIn("menu:home", callbacks)

    def test_clarification_notification_has_contextual_reply_action(self):
        buttons = flatten(keyboards.customer_notification_menu(12, 56, can_reply_to_clarification=True))
        self.assertEqual(len(buttons), 2)
        callbacks = [button.callback_data for button in buttons]
        self.assertIn("customer:open:12:56", callbacks)
        self.assertIn("customer:clarify:answer:12", callbacks)
        self.assertNotIn("menu:home", callbacks)

    def test_customer_order_card_has_no_separate_history_button(self):
        buttons = flatten(keyboards.my_order_actions(12, True))
        self.assertFalse(any("История" in button.text for button in buttons))
        self.assertIn("my:orders", [button.callback_data for button in buttons])

    def test_customer_order_card_shows_reply_only_when_open_clarification(self):
        no_request = flatten(keyboards.my_order_actions(12, True))
        with_request = flatten(keyboards.my_order_actions(12, True, has_open_clarification=True))
        self.assertFalse(any(button.callback_data == "customer:clarify:answer:12" for button in no_request))
        self.assertTrue(any(button.callback_data == "customer:clarify:answer:12" for button in with_request))

    def test_admin_order_card_changes_clarification_action_with_status(self):
        needs_info = flatten(keyboards.admin_order_menu(12, "needs_info"))
        completed = flatten(keyboards.admin_order_menu(12, "completed"))
        self.assertTrue(any(button.callback_data == "admin:clarify:view:12" for button in needs_info))
        self.assertFalse(any(button.callback_data == "admin:clarify:start:12" for button in needs_info))
        self.assertFalse(any("Уточнение" in button.text or "Запросить уточнение" in button.text for button in completed))


if __name__ == "__main__":
    unittest.main()
