import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.storage import OrderStorage


class OrderStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nested" / "orders.sqlite3"
        self.storage = OrderStorage(self.db_path)
        self.storage.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def create_order(self, user_id=123):
        return self.storage.create_order(
            user_id=user_id,
            username="demo_user",
            customer_name="Test User",
            service_id="telegram_bot",
            service_title="Telegram-бот под заказ",
            description="Нужен бот для сбора заявок",
            features="Админские уведомления, статусы",
            budget="5 000–15 000 ₽",
            deadline="Около недели",
            repository_url="https://github.com/example/project",
            contact="Telegram: @demo_user",
        )

    def test_duplicate_submission_key_returns_original_order(self):
        first_id = self.storage.create_order(
            user_id=123, username="demo_user", customer_name="Test User",
            service_id="telegram_bot", service_title="Telegram-бот под заказ",
            description="Нужен бот для сбора заявок", features="", budget="Не указан",
            deadline="Не указан", repository_url="", contact="Telegram", submission_key="same-submit-key",
        )
        second_id = self.storage.create_order(
            user_id=123, username="demo_user", customer_name="Test User",
            service_id="telegram_bot", service_title="Telegram-бот под заказ",
            description="Нужен бот для сбора заявок", features="", budget="Не указан",
            deadline="Не указан", repository_url="", contact="Telegram", submission_key="same-submit-key",
        )
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.storage.list_user_orders(123)), 1)

    def test_creates_and_reads_order(self):
        order_id = self.create_order()
        order = self.storage.get_order(order_id)
        self.assertIsNotNone(order)
        self.assertEqual(order["user_id"], 123)
        self.assertEqual(order["status"], "new")
        self.assertEqual(order["notification_status"], "pending")
        self.assertTrue(self.db_path.exists())

    def test_user_cannot_read_another_users_order(self):
        order_id = self.create_order(user_id=123)
        self.assertIsNone(self.storage.get_user_order(order_id, 999))
        self.assertEqual(len(self.storage.list_user_orders(999)), 0)
        self.assertEqual(len(self.storage.list_user_orders(123)), 1)

    def test_status_whitelist(self):
        order_id = self.create_order()
        self.assertTrue(self.storage.set_status(order_id, "in_progress"))
        self.assertEqual(self.storage.get_order(order_id)["status"], "in_progress")
        with self.assertRaises(ValueError):
            self.storage.set_status(order_id, "admin")

    def test_missing_order_status_returns_false(self):
        self.assertFalse(self.storage.set_status(9999, "completed"))

    def test_adds_internal_admin_note(self):
        order_id = self.create_order()
        self.assertTrue(self.storage.add_admin_note(order_id, 456, "Уточнить бюджет"))
        order = self.storage.get_order(order_id)
        self.assertIn("admin 456: Уточнить бюджет", order["admin_notes"])
        self.assertFalse(self.storage.add_admin_note(9999, 456, "Not there"))

    def test_invalid_notification_status_rejected(self):
        order_id = self.create_order()
        with self.assertRaises(ValueError):
            self.storage.set_notification_status(order_id, "leaked")

    def test_stats_export_and_pending_notifications(self):
        order_id = self.create_order()
        stats = self.storage.get_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(len(self.storage.list_pending_notifications()), 1)
        rows = self.storage.export_orders()
        self.assertEqual(rows[0]["id"], order_id)
        self.storage.set_notification_status(order_id, "sent")
        self.assertEqual(self.storage.list_pending_notifications(), [])


    def test_order_creation_adds_timestamped_customer_visible_event(self):
        order_id = self.create_order()
        events = self.storage.list_order_events(order_id, customer_visible_only=True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "created")
        self.assertEqual(events[0]["new_status"], "new")
        self.assertTrue(events[0]["created_at"])

    def test_status_transition_adds_event_and_rejects_invalid_jump(self):
        order_id = self.create_order()
        result = self.storage.transition_status(
            order_id, "under_review", actor_type="admin", actor_id=456,
            message="Заявка взята на рассмотрение.", customer_visible=True,
        )
        self.assertTrue(result["changed"])
        events = self.storage.list_order_events(order_id, customer_visible_only=True)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["old_status"], "new")
        self.assertEqual(events[-1]["new_status"], "under_review")
        self.assertEqual(events[-1]["actor_id"], 456)
        with self.assertRaises(ValueError):
            self.storage.transition_status(
                order_id, "completed", actor_type="admin", actor_id=456,
            )

    def test_same_status_does_not_create_duplicate_history(self):
        order_id = self.create_order()
        result = self.storage.transition_status(
            order_id, "new", actor_type="admin", actor_id=456,
        )
        self.assertFalse(result["changed"])
        self.assertEqual(len(self.storage.list_order_events(order_id)), 1)

    def test_internal_note_is_not_customer_visible(self):
        order_id = self.create_order()
        self.storage.add_admin_note(order_id, 456, "Внутренняя оценка бюджета")
        all_events = self.storage.list_order_events(order_id, customer_visible_only=False)
        customer_events = self.storage.list_order_events(order_id, customer_visible_only=True)
        self.assertEqual(len(all_events), 2)
        self.assertEqual(len(customer_events), 1)
        self.assertEqual(all_events[-1]["customer_visible"], 0)
        self.assertNotIn("Внутренняя оценка бюджета", [e["message"] for e in customer_events])

    def test_notification_delivery_is_per_admin_and_dismissal_keeps_order(self):
        order_id = self.create_order()
        self.storage.save_notification_delivery(
            order_id, 456, 456, status="sent", message_id=9001,
        )
        self.storage.save_notification_delivery(
            order_id, 789, 789, status="failed", error="TelegramForbiddenError",
        )
        admin_one = self.storage.get_notification_delivery(order_id, 456)
        admin_two = self.storage.get_notification_delivery(order_id, 789)
        self.assertEqual(admin_one["message_id"], 9001)
        self.assertEqual(admin_one["status"], "sent")
        self.assertEqual(admin_two["status"], "failed")
        dismissed = self.storage.dismiss_notification(order_id, 456)
        self.assertIsNotNone(dismissed["dismissed_at"])
        self.assertEqual(self.storage.get_notification_delivery(order_id, 456)["status"], "sent")
        self.assertIsNotNone(self.storage.get_order(order_id))
        self.assertEqual(self.storage.list_user_orders(123)[0]["id"], order_id)

    def test_notification_delivery_retry_increments_attempts(self):
        order_id = self.create_order()
        self.storage.save_notification_delivery(
            order_id, 456, 456, status="failed", error="TelegramNetworkError",
        )
        self.storage.save_notification_delivery(
            order_id, 456, 456, status="sent", message_id=9002,
        )
        row = self.storage.get_notification_delivery(order_id, 456)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["message_id"], 9002)
        self.assertEqual(row["last_error"], "")

    def test_user_panel_message_reference_can_be_updated(self):
        self.storage.set_user_ui_message(123, 123, 55)
        self.assertEqual(self.storage.get_user_ui_message(123)["panel_message_id"], 55)
        self.storage.set_user_ui_message(123, 123, 56)
        self.assertEqual(self.storage.get_user_ui_message(123)["panel_message_id"], 56)
        self.storage.clear_user_ui_message(123)
        self.assertIsNone(self.storage.get_user_ui_message(123))

    def test_event_history_window_returns_most_recent_events_in_chronological_order(self):
        order_id = self.create_order()
        for index in range(260):
            self.storage.add_event(
                order_id, actor_type="system", actor_id=None, event_type="progress",
                message=f"Тестовое событие {index}", customer_visible=True,
            )
        events = self.storage.list_order_events(order_id, limit=250)
        self.assertEqual(len(events), 250)
        self.assertEqual(events[0]["message"], "Тестовое событие 10")
        self.assertEqual(events[-1]["message"], "Тестовое событие 259")
        event_ids = [event["id"] for event in events]
        self.assertEqual(event_ids, sorted(event_ids))

    def test_existing_orders_table_gets_submission_key_migration(self):
        old_db = Path(self.temp.name) / "older-orders.sqlite3"
        with sqlite3.connect(old_db) as conn:
            conn.execute("""CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, username TEXT, customer_name TEXT NOT NULL,
                service_id TEXT NOT NULL, service_title TEXT NOT NULL, description TEXT NOT NULL,
                features TEXT NOT NULL DEFAULT '', budget TEXT NOT NULL DEFAULT 'Не указан',
                deadline TEXT NOT NULL DEFAULT 'Не указан', repository_url TEXT NOT NULL DEFAULT '',
                contact TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', admin_notes TEXT NOT NULL DEFAULT '',
                notification_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, legacy_lead_id INTEGER UNIQUE
            )""")
            conn.execute(
                """INSERT INTO orders
                (id,user_id,username,customer_name,service_id,service_title,description,features,budget,
                 deadline,repository_url,contact,status,admin_notes,notification_status,created_at,updated_at,legacy_lead_id)
                VALUES (77, 456, 'old_user', 'Existing order', 'telegram_bot', 'Old Bot',
                        'Keep this order during migration', '', 'Не указан', 'Не указан', '', '@old_user',
                        'in_progress', '', 'sent', '2026-02-01T10:00:00+00:00',
                        '2026-02-02T10:00:00+00:00', NULL)"""
            )
        conn.close()  # A sqlite3 connection context manager does not close the connection.
        storage = OrderStorage(old_db, journal_mode="DELETE")
        storage.initialize()
        storage.initialize()
        existing = storage.get_order(77)
        self.assertEqual(existing["customer_name"], "Existing order")
        self.assertEqual(existing["status"], "in_progress")
        history = storage.list_order_events(77)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["created_at"], "2026-02-01T10:00:00+00:00")
        order_id = storage.create_order(
            user_id=123, username=None, customer_name="Migrated", service_id="telegram_bot",
            service_title="Telegram Bot", description="Test migration", features="", budget="",
            deadline="", repository_url="", contact="Telegram", submission_key="after-migration",
        )
        self.assertEqual(storage.get_order(order_id)["customer_name"], "Migrated")
        with sqlite3.connect(old_db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        conn.close()
        self.assertIn("submission_key", columns)

    def test_price_assignment_writes_order_and_customer_visible_history(self):
        order_id = self.create_order()
        result = self.storage.set_order_price(order_id, "15000.50", "RUB", "Включает базовую разработку", admin_id=456)
        self.assertTrue(result["changed"])
        self.assertEqual(result["new_price"], "15000.50")
        saved = self.storage.get_order(order_id)
        self.assertEqual(saved["price_amount"], "15000.50")
        self.assertEqual(saved["price_currency"], "RUB")
        self.assertEqual(saved["price_note"], "Включает базовую разработку")
        events = self.storage.list_order_events(order_id, customer_visible_only=True)
        price_events = [event for event in events if event["event_type"] == "price_assigned"]
        self.assertEqual(len(price_events), 1)
        self.assertIn("15000.50 RUB", price_events[0]["message"])
        unchanged = self.storage.set_order_price(order_id, "15000.50", "RUB", "Включает базовую разработку", admin_id=456)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(len([event for event in self.storage.list_order_events(order_id) if event["event_type"] == "price_assigned"]), 1)

    def test_price_assignment_rejects_invalid_values(self):
        order_id = self.create_order()
        for bad in ("-10", "0", "NaN", "Infinity", "oops", "10.999"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.storage.set_order_price(order_id, bad, "RUB", "", admin_id=456)

    def test_admin_ui_panel_reference_survives_restarts_and_can_be_replaced(self):
        self.storage.set_admin_ui_message(456, 456, 1001)
        self.assertEqual(
            self.storage.get_admin_ui_message(456),
            {"admin_id": 456, "chat_id": 456, "panel_message_id": 1001,
             "updated_at": self.storage.get_admin_ui_message(456)["updated_at"]},
        )
        self.storage.set_admin_ui_message(456, 456, 1002)
        self.assertEqual(self.storage.get_admin_ui_message(456)["panel_message_id"], 1002)
        self.storage.clear_admin_ui_message(456)
        self.assertIsNone(self.storage.get_admin_ui_message(456))

    def test_find_customer_notification_by_message_id_is_scoped_to_owner(self):
        order_id = self.create_order(user_id=123)
        notification_id = self.storage.create_customer_notification(
            event_key="event-message-lookup", order_id=order_id, user_id=123,
            notification_type="status_changed", body="Статус обновлён",
        )
        self.storage.save_customer_notification_delivery(notification_id, status="sent", message_id=654)
        self.assertEqual(
            self.storage.get_customer_notification_by_message(123, 654)["id"], notification_id
        )
        self.assertIsNone(self.storage.get_customer_notification_by_message(999, 654))
        self.assertIsNone(self.storage.get_customer_notification_by_message(123, 999))

    def test_customer_notification_is_idempotent_and_tracks_processing(self):
        order_id = self.create_order()
        kwargs = dict(
            event_key=f"status:{order_id}:event-1", order_id=order_id, user_id=123,
            notification_type="status_changed", body="Заявка обновлена",
        )
        first = self.storage.create_customer_notification(**kwargs)
        second = self.storage.create_customer_notification(**kwargs)
        self.assertEqual(first, second)
        row = self.storage.get_customer_notification(first)
        self.assertEqual(row["status"], "pending")
        self.storage.save_customer_notification_delivery(first, status="sent", message_id=987)
        visible = self.storage.list_customer_notifications(123, order_id=order_id, statuses=("sent",))
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["message_id"], 987)
        self.storage.save_customer_notification_delivery(first, status="processed")
        self.assertEqual(self.storage.get_customer_notification(first)["status"], "processed")
        self.assertEqual(self.storage.list_customer_notifications(123, order_id=order_id, statuses=("sent",)), [])

    def test_order_draft_persists_and_can_be_replaced_or_deleted(self):
        self.storage.save_order_draft(
            123, "OrderForm:description", {"service_id": "telegram_bot", "description": "A demo request"},
            chat_id=123, panel_message_id=456,
        )
        draft = self.storage.get_order_draft(123)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["state_name"], "OrderForm:description")
        self.assertEqual(draft["data"]["description"], "A demo request")
        self.assertEqual(draft["panel_message_id"], 456)
        self.storage.save_order_draft(
            123, "OrderForm:features", {"service_id": "telegram_bot", "features": "Database"},
            chat_id=123, panel_message_id=789,
        )
        updated = self.storage.get_order_draft(123)
        self.assertEqual(updated["state_name"], "OrderForm:features")
        self.assertEqual(updated["panel_message_id"], 789)
        self.storage.delete_order_draft(123)
        self.assertIsNone(self.storage.get_order_draft(123))

    def test_pending_customer_notifications_can_be_retried(self):
        order_id = self.create_order()
        notification_id = self.storage.create_customer_notification(
            event_key="event-retry", order_id=order_id, user_id=123,
            notification_type="price_assigned", body="Цена назначена",
        )
        self.storage.save_customer_notification_delivery(notification_id, status="failed", error="TelegramAPIError")
        pending = self.storage.list_pending_customer_notifications()
        self.assertEqual([row["id"] for row in pending], [notification_id])

    def test_legacy_leads_import_once(self):
        legacy_db = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("""CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT,
                category TEXT NOT NULL, customer_name TEXT NOT NULL, contact TEXT NOT NULL,
                description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL
            )""")
            conn.execute(
                "INSERT INTO leads(user_id, username, category, customer_name, contact, description, status, created_at) VALUES(1,'old_user','service','Old User','@old_user','Legacy request','in_progress','2026-01-01T12:00:00+00:00')"
            )
        conn.close()  # Release the file before OrderStorage opens it on Windows.
        storage = OrderStorage(legacy_db, journal_mode="DELETE")
        storage.initialize()
        storage.initialize()
        orders = storage.list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["customer_name"], "Old User")
        self.assertEqual(orders[0]["status"], "in_progress")


    def test_create_clarification_moves_order_and_records_customer_visible_question(self):
        order_id = self.create_order()
        clarification = self.storage.create_clarification(order_id, 456, "Какой сервис должен использовать бот?")
        self.assertIsNotNone(clarification)
        self.assertEqual(clarification["state"], "open")
        self.assertEqual(clarification["user_id"], 123)
        self.assertEqual(self.storage.get_order(order_id)["status"], "needs_info")
        events = self.storage.list_order_events(order_id, customer_visible_only=True)
        self.assertEqual(events[-1]["event_type"], "clarification_requested")
        self.assertIn("Какой сервис", events[-1]["message"])

    def test_only_one_open_clarification_allowed_per_order(self):
        order_id = self.create_order()
        self.storage.create_clarification(order_id, 456, "Первый вопрос клиенту?")
        with self.assertRaises(ValueError):
            self.storage.create_clarification(order_id, 456, "Второй вопрос клиенту?")

    def test_answer_clarification_closes_request_and_returns_order_to_review(self):
        order_id = self.create_order()
        clarification = self.storage.create_clarification(order_id, 456, "Какой сценарий нужен?")
        result = self.storage.answer_clarification(
            clarification["id"], order_id, 123, "Нужен сценарий обработки заказов."
        )
        self.assertEqual(result["new_status"], "under_review")
        self.assertIsNone(self.storage.get_active_clarification(order_id))
        self.assertEqual(self.storage.get_order(order_id)["status"], "under_review")
        events = self.storage.list_order_events(order_id, customer_visible_only=True)
        types = [event["event_type"] for event in events]
        self.assertLess(types.index("clarification_answered"), len(types) - 1)
        self.assertEqual(types[-1], "status_changed")
        self.assertIn("Нужен сценарий", events[-2]["message"])

    def test_customer_cannot_answer_another_users_clarification(self):
        order_id = self.create_order(user_id=123)
        clarification = self.storage.create_clarification(order_id, 456, "Какой формат нужен?")
        self.assertIsNone(self.storage.answer_clarification(
            clarification["id"], order_id, 999, "Чужой ответ"
        ))
        self.assertIsNotNone(self.storage.get_active_clarification(order_id))


if __name__ == "__main__":
    unittest.main()
