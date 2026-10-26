"""Static regression checks for handler-level contracts; not a Telegram integration test."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_SOURCE = (ROOT / "bot.py").read_text(encoding="utf-8")
KEYBOARD_SOURCE = (ROOT / "app" / "keyboards.py").read_text(encoding="utf-8")
TREE = ast.parse(BOT_SOURCE)


def function_source(name: str) -> str:
    node = next((item for item in TREE.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name), None)
    if node is None:
        raise AssertionError(f"Function {name!r} was not found")
    return ast.get_source_segment(BOT_SOURCE, node) or ""


class BotSourceContractTests(unittest.TestCase):
    def test_non_admin_admin_command_has_no_denial_message(self):
        source = function_source("admin_command")
        self.assertIn("message.from_user.id not in ADMIN_IDS", source)
        self.assertIn("return", source)
        self.assertNotIn("Доступ запрещён", source)
        self.assertNotIn("message.answer", source)
        self.assertIn("dp.message.outer_middleware(PrivateMessageCleanupMiddleware())", BOT_SOURCE)

    def test_unmatched_private_messages_have_a_silent_fallback(self):
        self.assertIn("async def ignore_unhandled_private_message", BOT_SOURCE)
        self.assertIn("@dp.message(F.chat.type == ChatType.PRIVATE)", BOT_SOURCE)
        self.assertIn("dp.message.outer_middleware(PrivateMessageCleanupMiddleware())", BOT_SOURCE)

    def test_client_history_button_and_handler_are_removed(self):
        self.assertNotIn("my:history:", KEYBOARD_SOURCE)
        self.assertNotIn('F.data.startswith("my:history:")', BOT_SOURCE)

    def test_notification_types_use_common_outbox_delivery(self):
        self.assertIn('notification_type="status_changed"', BOT_SOURCE)
        self.assertIn('notification_type="price_assigned"', BOT_SOURCE)
        self.assertIn('notification_type="clarification_requested"', BOT_SOURCE)
        self.assertIn('notification_type="admin_reply"', BOT_SOURCE)
        self.assertIn("async def deliver_customer_notification", BOT_SOURCE)

    def test_admin_ui_is_persisted_and_reused(self):
        self.assertIn("async def render_admin_panel", BOT_SOURCE)
        self.assertIn("STORAGE.get_admin_ui_message", BOT_SOURCE)
        self.assertIn("STORAGE.set_admin_ui_message", BOT_SOURCE)

    def test_notification_is_marked_processed_after_destination_render(self):
        source = function_source("open_customer_notification")
        self.assertLess(source.index("await query.message.edit_text"), source.index("await mark_notification_message_processed"))
        self.assertIn("await state.clear()", source)

    def test_invalid_customer_form_input_updates_existing_panel(self):
        for name in ("get_description", "get_features", "get_custom_budget", "get_repository_url", "get_custom_contact"):
            source = function_source(name)
            self.assertIn("answer_panel", source, f"{name} should use the tracked panel")

    def test_customer_screens_sync_panel_reference_after_edits(self):
        names = (
            "choose_budget", "open_edit_menu", "edit_service", "edit_description",
            "edit_features", "edit_budget", "edit_deadline", "show_services",
            "show_service_detail", "show_portfolio", "show_contact", "ask_cancel_my_order",
        )
        for name in names:
            self.assertIn("remember_panel", function_source(name), f"{name} must sync the active panel")

    def test_incoming_cleanup_runs_after_successful_private_message_handler(self):
        start = BOT_SOURCE.index("class PrivateMessageCleanupMiddleware")
        end = BOT_SOURCE.index("dp.message.outer_middleware(PrivateMessageCleanupMiddleware())", start)
        source = BOT_SOURCE[start:end]
        self.assertLess(source.index("result = await handler(event, data)"), source.index("await delete_user_message(event)"))
        self.assertIn("event.chat.type == ChatType.PRIVATE", source)

    def test_notification_panel_retirement_preserves_newly_delivered_notification(self):
        source = function_source("remember_panel")
        self.assertIn("keep_notification_id", source)
        self.assertIn("save_customer_notification_delivery", source)
        delivery = function_source("deliver_customer_notification")
        self.assertIn("keep_notification_id=notification_id", delivery)


if __name__ == "__main__":
    unittest.main()
