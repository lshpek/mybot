"""SQLite persistence for BOTVEX orders, event history and per-admin notifications.

This module intentionally uses only the Python standard library. Call blocking
methods through asyncio.to_thread() from aiogram handlers.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOWED_STATUSES = {
    "new", "under_review", "needs_info", "quote_sent", "awaiting_client",
    "in_progress", "completed", "cancelled",
}
ALLOWED_NOTIFICATION_STATES = {"pending", "sent", "failed"}

# Explicit transitions prevent forged callbacks and accidental jumps, while allowing
# deliberate reopening of completed/cancelled orders for corrections.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new": {"under_review", "needs_info", "quote_sent", "awaiting_client", "in_progress", "cancelled"},
    "under_review": {"needs_info", "quote_sent", "awaiting_client", "in_progress", "cancelled"},
    "needs_info": {"under_review", "quote_sent", "awaiting_client", "in_progress", "cancelled"},
    "quote_sent": {"under_review", "awaiting_client", "in_progress", "cancelled"},
    "awaiting_client": {"under_review", "needs_info", "in_progress", "cancelled"},
    "in_progress": {"needs_info", "completed", "cancelled"},
    "completed": {"under_review"},
    "cancelled": {"under_review"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrderStorage:
    def __init__(self, path: str | Path, *, journal_mode: str = "WAL") -> None:
        self.path = str(path)
        # Production defaults to WAL. DELETE mode is available for test/legacy
        # migration databases where Windows file cleanup can be sensitive to WAL handles.
        normalized_mode = journal_mode.upper()
        if normalized_mode not in {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}:
            raise ValueError(f"Unsupported SQLite journal mode: {journal_mode}")
        self.journal_mode = normalized_mode

    def _connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        if self.path != ":memory:":
            conn.execute(f"PRAGMA journal_mode = {self.journal_mode}")
        return conn

    @contextmanager
    def _connection(self):
        """Commit/rollback transaction scope and always close the SQLite connection."""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def initialize(self) -> None:
        """Create schema and apply idempotent, non-destructive migrations."""
        with self._connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    customer_name TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    service_title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    features TEXT NOT NULL DEFAULT '',
                    budget TEXT NOT NULL DEFAULT 'Не указан',
                    deadline TEXT NOT NULL DEFAULT 'Не указан',
                    repository_url TEXT NOT NULL DEFAULT '',
                    contact TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new','under_review','needs_info','quote_sent',
                                          'awaiting_client','in_progress','completed','cancelled')),
                    admin_notes TEXT NOT NULL DEFAULT '',
                    notification_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (notification_status IN ('pending','sent','failed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    legacy_lead_id INTEGER UNIQUE,
                    submission_key TEXT
                )"""
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "submission_key" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN submission_key TEXT")
            if "price_amount" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN price_amount TEXT")
            if "price_currency" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN price_currency TEXT NOT NULL DEFAULT 'RUB'")
            if "price_note" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN price_note TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_submission_key "
                "ON orders(submission_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, id DESC)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    actor_type TEXT NOT NULL,
                    actor_id INTEGER,
                    event_type TEXT NOT NULL,
                    old_status TEXT,
                    new_status TEXT,
                    message TEXT NOT NULL,
                    customer_visible INTEGER NOT NULL DEFAULT 1 CHECK (customer_visible IN (0,1)),
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_events_order_created "
                "ON order_events(order_id, id ASC)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    admin_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','sent','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    dismissed_at TEXT,
                    sent_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(order_id, admin_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_delivery_status "
                "ON notification_deliveries(status, order_id)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS customer_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    notification_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','sent','failed','processed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer_notifications_user_status "
                "ON customer_notifications(user_id, status, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer_notifications_order_status "
                "ON customer_notifications(order_id, status, id DESC)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS order_clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    admin_user_id INTEGER NOT NULL,
                    question_text TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    answered_at_utc TEXT,
                    answer_text TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open','answered','cancelled'))
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_clarifications_order_state "
                "ON order_clarifications(order_id, state, id DESC)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS order_drafts (
                    user_id INTEGER PRIMARY KEY,
                    state_name TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    panel_message_id INTEGER,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS user_ui_state (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    panel_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS admin_ui_state (
                    admin_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    panel_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

            # One-time migration from the previous BOTVEX Lead Demo's leads table.
            if self._table_exists(conn, "leads"):
                legacy_rows = conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
                legacy_service = {
                    "service": ("telegram_bot", "Telegram-бот / разработка"),
                    "auto": ("telegram_bot", "Telegram-бот для автосервиса"),
                    "order": ("custom", "Заказ / другая задача"),
                    "question": ("custom", "Вопрос по разработке"),
                }
                for row in legacy_rows:
                    if conn.execute(
                        "SELECT 1 FROM orders WHERE legacy_lead_id = ?", (row["id"],)
                    ).fetchone():
                        continue
                    service_id, service_title = legacy_service.get(
                        row["category"], ("custom", str(row["category"]))
                    )
                    old_status = row["status"]
                    mapped_status = {
                        "new": "new", "in_progress": "in_progress", "completed": "completed",
                        "cancelled": "cancelled", "under_review": "under_review",
                    }.get(old_status, "new")
                    created_at = row["created_at"] or utc_now()
                    cur = conn.execute(
                        """INSERT INTO orders (
                            user_id, username, customer_name, service_id, service_title,
                            description, features, budget, deadline, repository_url, contact,
                            status, notification_status, created_at, updated_at, legacy_lead_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?, ?)""",
                        (
                            row["user_id"], row["username"], row["customer_name"], service_id,
                            service_title, row["description"], "Перенесено из предыдущего демо",
                            "Не указан", "Не указан", "", row["contact"], mapped_status,
                            created_at, created_at, row["id"],
                        ),
                    )
                    order_id = int(cur.lastrowid)
                    conn.execute(
                        """INSERT INTO order_events
                           (order_id, actor_type, actor_id, event_type, old_status, new_status,
                            message, customer_visible, created_at)
                           VALUES (?, 'system', NULL, 'created', NULL, ?, ?, 1, ?)""",
                        (order_id, mapped_status, "Заявка перенесена из предыдущего демо.", created_at),
                    )

            # Historical orders keep their original IDs and timestamps. Their only
            # trustworthy legacy event is creation, so do not invent intermediate steps.
            conn.execute(
                """INSERT INTO order_events
                   (order_id, actor_type, actor_id, event_type, old_status, new_status,
                    message, customer_visible, created_at)
                   SELECT o.id, 'system', NULL, 'created', NULL, o.status,
                          'Заявка создана.', 1, o.created_at
                   FROM orders o
                   WHERE NOT EXISTS (
                     SELECT 1 FROM order_events e WHERE e.order_id = o.id
                   )"""
            )

    def create_order(
        self,
        *,
        user_id: int,
        username: str | None,
        customer_name: str,
        service_id: str,
        service_title: str,
        description: str,
        features: str,
        budget: str,
        deadline: str,
        repository_url: str,
        contact: str,
        submission_key: str | None = None,
    ) -> int:
        now = utc_now()
        with self._connection() as conn:
            if submission_key:
                existing = conn.execute(
                    "SELECT id FROM orders WHERE submission_key = ?", (submission_key,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cur = conn.execute(
                """INSERT INTO orders (
                    user_id, username, customer_name, service_id, service_title,
                    description, features, budget, deadline, repository_url, contact,
                    status, notification_status, created_at, updated_at, submission_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 'pending', ?, ?, ?)
                ON CONFLICT(submission_key) DO NOTHING""",
                (
                    user_id, username, customer_name, service_id, service_title,
                    description, features, budget, deadline, repository_url, contact,
                    now, now, submission_key,
                ),
            )
            if cur.rowcount == 1:
                order_id = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO order_events
                       (order_id, actor_type, actor_id, event_type, old_status, new_status,
                        message, customer_visible, created_at)
                       VALUES (?, 'customer', ?, 'created', NULL, 'new', 'Заявка создана.', 1, ?)""",
                    (order_id, user_id, now),
                )
                return order_id
            if submission_key:
                existing = conn.execute(
                    "SELECT id FROM orders WHERE submission_key = ?", (submission_key,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            raise RuntimeError("Order was not created and no matching submission key was found")

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def get_user_order(self, order_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT o.*,
                          (SELECT e.created_at FROM order_events e
                           WHERE e.order_id = o.id AND e.customer_visible = 1
                           ORDER BY e.id DESC LIMIT 1) AS customer_updated_at
                   FROM orders o WHERE o.id = ? AND o.user_id = ?""",
                (order_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_user_order(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT o.*,
                          (SELECT e.created_at FROM order_events e
                           WHERE e.order_id = o.id AND e.customer_visible = 1
                           ORDER BY e.id DESC LIMIT 1) AS customer_updated_at
                   FROM orders o WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_user_orders(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT o.*,
                          (SELECT e.created_at FROM order_events e
                           WHERE e.order_id = o.id AND e.customer_visible = 1
                           ORDER BY e.id DESC LIMIT 1) AS customer_updated_at
                   FROM orders o WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?""",
                (user_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_orders(
        self, limit: int = 10, offset: int = 0, status: str | None = None,
        notifications_only: bool = False,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in ALLOWED_STATUSES:
                raise ValueError(f"Unsupported order status: {status}")
            clauses.append("status = ?")
            params.append(status)
        if notifications_only:
            clauses.append("notification_status IN ('pending','failed')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([safe_limit, safe_offset])
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ? OFFSET ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def count_orders(self, status: str | None = None, notifications_only: bool = False) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in ALLOWED_STATUSES:
                raise ValueError(f"Unsupported order status: {status}")
            clauses.append("status = ?")
            params.append(status)
        if notifications_only:
            clauses.append("notification_status IN ('pending','failed')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM orders {where}", params).fetchone()[0])

    def transition_status(
        self, order_id: int, status: str, *, actor_type: str, actor_id: int | None,
        message: str | None = None, customer_visible: bool = True,
    ) -> dict[str, Any] | None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported order status: {status}")
        now = utc_now()
        with self._connection() as conn:
            row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                return None
            old_status = str(row["status"])
            if status == old_status:
                return {"changed": False, "old_status": old_status, "new_status": status}
            if status not in ALLOWED_TRANSITIONS.get(old_status, set()):
                raise ValueError(f"Transition from {old_status} to {status} is not allowed")
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, order_id),
            )
            event_message = message or f"Статус изменён: {old_status} → {status}."
            conn.execute(
                """INSERT INTO order_events
                   (order_id, actor_type, actor_id, event_type, old_status, new_status,
                    message, customer_visible, created_at)
                   VALUES (?, ?, ?, 'status_changed', ?, ?, ?, ?, ?)""",
                (order_id, actor_type, actor_id, old_status, status, event_message,
                 int(customer_visible), now),
            )
        return {"changed": True, "old_status": old_status, "new_status": status, "created_at": now}

    def set_order_price(
        self, order_id: int, amount: str, currency: str, note: str, *, admin_id: int,
    ) -> dict[str, Any] | None:
        """Set a quoted price atomically and append an auditable customer-visible event."""
        try:
            normalized = Decimal(str(amount).replace(",", ".").strip())
        except (InvalidOperation, ValueError):
            raise ValueError("Price must be a positive decimal number") from None
        if not normalized.is_finite() or normalized <= 0 or normalized > Decimal("1000000000"):
            raise ValueError("Price must be positive and within the supported range")
        if normalized != normalized.quantize(Decimal("0.01")):
            raise ValueError("Price must have at most two decimal places")
        normalized = normalized.quantize(Decimal("0.01"))
        amount_text = format(normalized, "f")
        currency = str(currency).strip().upper()
        if not currency or len(currency) > 8:
            raise ValueError("Currency is invalid")
        note = str(note or "").strip()
        if len(note) > 1000:
            raise ValueError("Price note exceeds 1000 characters")
        now = utc_now()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT user_id, price_amount, price_currency, price_note FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                return None
            previous = row["price_amount"]
            previous_currency = row["price_currency"] or "RUB"
            previous_note = row["price_note"] or ""
            if str(previous or "") == amount_text and previous_currency == currency and previous_note == note:
                return {"changed": False, "old_price": previous, "new_price": amount_text,
                        "currency": currency, "user_id": int(row["user_id"])}
            conn.execute(
                "UPDATE orders SET price_amount = ?, price_currency = ?, price_note = ?, updated_at = ? WHERE id = ?",
                (amount_text, currency, note, now, order_id),
            )
            verb = "Стоимость назначена" if previous is None else "Стоимость изменена"
            message = f"{verb}: {amount_text} {currency}."
            if note:
                message += f" Пояснение: {note}"
            cursor = conn.execute(
                """INSERT INTO order_events
                   (order_id, actor_type, actor_id, event_type, old_status, new_status,
                    message, customer_visible, created_at)
                   VALUES (?, 'admin', ?, ?, NULL, NULL, ?, 1, ?)""",
                (order_id, admin_id, "price_assigned" if previous is None else "price_updated", message, now),
            )
            return {
                "changed": True, "old_price": previous, "new_price": amount_text,
                "old_currency": previous_currency, "currency": currency, "note": note,
                "user_id": int(row["user_id"]), "created_at": now, "event_id": int(cursor.lastrowid),
            }

    def get_active_clarification(self, order_id: int) -> dict[str, Any] | None:
        """Return the newest unanswered clarification for an order."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM order_clarifications WHERE order_id = ? AND state = 'open' ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_clarification(self, order_id: int, admin_user_id: int, question_text: str) -> dict[str, Any] | None:
        """Create one active clarification and move its order to needs_info atomically."""
        question = str(question_text or '').strip()
        if len(question) < 3 or len(question) > 1500:
            raise ValueError('Clarification question must contain 3–1500 characters')
        now = utc_now()
        with self._connection() as conn:
            order = conn.execute('SELECT user_id, status FROM orders WHERE id = ?', (order_id,)).fetchone()
            if order is None:
                return None
            active = conn.execute(
                "SELECT id FROM order_clarifications WHERE order_id = ? AND state = 'open' ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if active:
                raise ValueError('An unanswered clarification already exists for this order')
            old_status = str(order['status'])
            if old_status != 'needs_info' and 'needs_info' not in ALLOWED_TRANSITIONS.get(old_status, set()):
                raise ValueError(f'Order status {old_status} cannot request clarification')
            if old_status != 'needs_info':
                conn.execute('UPDATE orders SET status = ?, updated_at = ? WHERE id = ?', ('needs_info', now, order_id))
            cur = conn.execute(
                """INSERT INTO order_clarifications
                   (order_id, admin_user_id, question_text, created_at_utc, state)
                   VALUES (?, ?, ?, ?, 'open')""",
                (order_id, admin_user_id, question, now),
            )
            conn.execute(
                """INSERT INTO order_events
                   (order_id, actor_type, actor_id, event_type, old_status, new_status,
                    message, customer_visible, created_at)
                   VALUES (?, 'admin', ?, 'clarification_requested', ?, 'needs_info', ?, 1, ?)""",
                (order_id, admin_user_id, old_status, f'Запрошено уточнение: {question}', now),
            )
            conn.execute('UPDATE orders SET updated_at = ? WHERE id = ?', (now, order_id))
            row = conn.execute('SELECT * FROM order_clarifications WHERE id = ?', (cur.lastrowid,)).fetchone()
        result = dict(row)
        result['user_id'] = int(order['user_id'])
        result['old_status'] = old_status
        result['new_status'] = 'needs_info'
        result['status_changed'] = old_status != 'needs_info'
        return result

    def answer_clarification(
        self, clarification_id: int, order_id: int, user_id: int, answer_text: str,
    ) -> dict[str, Any] | None:
        """Save an owner response and close the active clarification atomically."""
        answer = str(answer_text or '').strip()
        if not answer or len(answer) > 3000:
            raise ValueError('Clarification answer must contain 1–3000 characters')
        now = utc_now()
        with self._connection() as conn:
            row = conn.execute(
                """SELECT c.*, o.user_id AS owner_id, o.status AS order_status
                   FROM order_clarifications c JOIN orders o ON o.id = c.order_id
                   WHERE c.id = ? AND c.order_id = ? AND o.user_id = ? AND o.status = 'needs_info'""",
                (clarification_id, order_id, user_id),
            ).fetchone()
            if row is None:
                return None
            if row['state'] != 'open':
                raise ValueError('This clarification is no longer open')
            old_status = str(row['order_status'])
            new_status = old_status
            # Insert the answer event before the status transition for a natural timeline.
            conn.execute(
                """INSERT INTO order_events
                   (order_id, actor_type, actor_id, event_type, old_status, new_status,
                    message, customer_visible, created_at)
                   VALUES (?, 'customer', ?, 'clarification_answered', NULL, NULL, ?, 1, ?)""",
                (order_id, user_id, f'Ответ на уточнение: {answer}', now),
            )
            conn.execute(
                """UPDATE order_clarifications SET state = 'answered', answer_text = ?, answered_at_utc = ?
                   WHERE id = ? AND state = 'open'""",
                (answer, now, clarification_id),
            )
            if old_status == 'needs_info' and 'under_review' in ALLOWED_TRANSITIONS.get(old_status, set()):
                new_status = 'under_review'
                conn.execute('UPDATE orders SET status = ?, updated_at = ? WHERE id = ?', (new_status, now, order_id))
                conn.execute(
                    """INSERT INTO order_events
                       (order_id, actor_type, actor_id, event_type, old_status, new_status,
                        message, customer_visible, created_at)
                       VALUES (?, 'system', NULL, 'status_changed', ?, ?, ?, 1, ?)""",
                    (order_id, old_status, new_status, 'Ответ клиента получен; заявка возвращена на рассмотрение.', now),
                )
            conn.execute('UPDATE orders SET updated_at = ? WHERE id = ?', (now, order_id))
            order = conn.execute('SELECT user_id, status FROM orders WHERE id = ?', (order_id,)).fetchone()
        return {
            'clarification_id': clarification_id, 'order_id': order_id,
            'user_id': int(order['user_id']), 'old_status': old_status,
            'new_status': str(order['status']), 'changed': old_status != str(order['status']),
            'answer_text': answer, 'created_at': now,
        }

    def cancel_open_clarification(self, order_id: int) -> bool:
        """Cancel any open clarification when an administrator changes workflow stage."""
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE order_clarifications SET state = 'cancelled', answered_at_utc = ? "
                "WHERE order_id = ? AND state = 'open'",
                (utc_now(), order_id),
            )
            return cur.rowcount > 0

    def create_customer_notification(
        self, *, event_key: str, order_id: int, user_id: int,
        notification_type: str, body: str,
    ) -> int:
        """Create an idempotent durable customer notification/outbox entry."""
        now = utc_now()
        if not event_key or len(event_key) > 200:
            raise ValueError("Invalid customer notification event key")
        if not body or len(body) > 3500:
            raise ValueError("Customer notification body must contain 1–3500 characters")
        with self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO customer_notifications
                   (event_key, order_id, user_id, notification_type, body, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (event_key, order_id, user_id, notification_type, body, now, now),
            )
            row = conn.execute(
                "SELECT id FROM customer_notifications WHERE event_key = ?", (event_key,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Customer notification could not be created")
            return int(row["id"])

    def get_customer_notification(self, notification_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM customer_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_customer_notification_delivery(
        self, notification_id: int, *, status: str, message_id: int | None = None,
        error: str = "",
    ) -> None:
        if status not in {"sent", "failed", "processed", "pending"}:
            raise ValueError(f"Unsupported customer notification status: {status}")
        now = utc_now()
        with self._connection() as conn:
            conn.execute(
                """UPDATE customer_notifications
                   SET status = ?, message_id = COALESCE(?, message_id),
                       attempts = attempts + CASE WHEN ? IN ('sent','failed') THEN 1 ELSE 0 END,
                       last_error = ?, updated_at = ?,
                       processed_at = CASE WHEN ? = 'processed' THEN ? ELSE processed_at END
                   WHERE id = ?""",
                (status, message_id, status, error[:200], now, status, now, notification_id),
            )

    def list_customer_notifications(
        self, user_id: int, *, order_id: int | None = None, limit: int = 100,
        statuses: tuple[str, ...] = ("sent",),
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        allowed = {"pending", "sent", "failed", "processed"}
        normalized = tuple(status for status in statuses if status in allowed)
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        params: list[Any] = [user_id, *normalized]
        where = f"user_id = ? AND status IN ({placeholders})"
        if order_id is not None:
            where += " AND order_id = ?"
            params.append(order_id)
        params.append(safe_limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM customer_notifications WHERE {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pending_customer_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM customer_notifications WHERE status IN ('pending','failed') ORDER BY id ASC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, order_id: int, status: str) -> bool:
        """Backward-compatible wrapper for existing callers/tests."""
        result = self.transition_status(
            order_id, status, actor_type="system", actor_id=None,
            message=f"Статус изменён: {status}.", customer_visible=True,
        )
        return result is not None

    def add_event(
        self, order_id: int, *, actor_type: str, actor_id: int | None,
        event_type: str, message: str, customer_visible: bool = True,
        old_status: str | None = None, new_status: str | None = None,
    ) -> bool:
        if not message.strip() or len(message) > 3000:
            raise ValueError("Event message must contain 1–3000 characters")
        with self._connection() as conn:
            exists = conn.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not exists:
                return False
            conn.execute(
                """INSERT INTO order_events
                   (order_id, actor_type, actor_id, event_type, old_status, new_status,
                    message, customer_visible, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, actor_type, actor_id, event_type, old_status, new_status,
                 message.strip(), int(customer_visible), utc_now()),
            )
            return True

    def list_order_events(
        self, order_id: int, *, customer_visible_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 250))
        clause = " AND customer_visible = 1" if customer_visible_only else ""
        # Select the newest window first, then reverse it for chronological display.
        # ORDER BY id is the authoritative event sequence even when timestamps match.
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM order_events WHERE order_id = ?{clause} ORDER BY id DESC LIMIT ?",
                (order_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def add_admin_note(self, order_id: int, admin_id: int, note: str) -> bool:
        note = note.strip()
        if not note or len(note) > 2000:
            raise ValueError("Note must contain 1–2000 characters")
        stamp = utc_now()
        log_line = f"[{stamp}] admin {admin_id}: {note}"
        with self._connection() as conn:
            cur = conn.execute(
                """UPDATE orders
                   SET admin_notes = CASE WHEN admin_notes = '' THEN ?
                     ELSE admin_notes || char(10) || ? END,
                       updated_at = ?
                   WHERE id = ?""",
                (log_line, log_line, stamp, order_id),
            )
            if cur.rowcount == 1:
                conn.execute(
                    """INSERT INTO order_events
                       (order_id, actor_type, actor_id, event_type, old_status, new_status,
                        message, customer_visible, created_at)
                       VALUES (?, 'admin', ?, 'internal_note', NULL, NULL, ?, 0, ?)""",
                    (order_id, admin_id, "Добавлена внутренняя заметка.", stamp),
                )
                return True
            return False

    def set_notification_status(self, order_id: int, status: str) -> None:
        if status not in ALLOWED_NOTIFICATION_STATES:
            raise ValueError(f"Unsupported notification status: {status}")
        with self._connection() as conn:
            conn.execute(
                "UPDATE orders SET notification_status = ? WHERE id = ?", (status, order_id)
            )

    def get_notification_delivery(self, order_id: int, admin_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM notification_deliveries WHERE order_id = ? AND admin_id = ?",
                (order_id, admin_id),
            ).fetchone()
        return dict(row) if row else None

    def save_notification_delivery(
        self, order_id: int, admin_id: int, chat_id: int, *, status: str,
        message_id: int | None = None, error: str = "",
    ) -> None:
        if status not in ALLOWED_NOTIFICATION_STATES:
            raise ValueError(f"Unsupported notification status: {status}")
        now = utc_now()
        safe_error = error[:500]
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO notification_deliveries
                   (order_id, admin_id, chat_id, message_id, status, attempts, last_error,
                    dismissed_at, sent_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)
                   ON CONFLICT(order_id, admin_id) DO UPDATE SET
                     chat_id = excluded.chat_id,
                     message_id = COALESCE(excluded.message_id, notification_deliveries.message_id),
                     status = excluded.status,
                     attempts = notification_deliveries.attempts + 1,
                     last_error = excluded.last_error,
                     dismissed_at = CASE WHEN excluded.status = 'sent' THEN NULL ELSE notification_deliveries.dismissed_at END,
                     sent_at = CASE WHEN excluded.status = 'sent' THEN excluded.sent_at ELSE notification_deliveries.sent_at END,
                     updated_at = excluded.updated_at""",
                (order_id, admin_id, chat_id, message_id, status, safe_error,
                 now if status == "sent" else None, now),
            )

    def dismiss_notification(self, order_id: int, admin_id: int) -> dict[str, Any] | None:
        now = utc_now()
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE notification_deliveries SET dismissed_at = ?, updated_at = ? "
                "WHERE order_id = ? AND admin_id = ?",
                (now, now, order_id, admin_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM notification_deliveries WHERE order_id = ? AND admin_id = ?",
                (order_id, admin_id),
            ).fetchone()
        return dict(row) if row else None

    def list_pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM orders
                   WHERE notification_status IN ('pending', 'failed')
                   ORDER BY id ASC LIMIT ?""",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM orders GROUP BY status").fetchall()
            total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE notification_status IN ('pending','failed')"
            ).fetchone()[0]
        result = {str(row["status"]): int(row["count"]) for row in rows}
        result["total"] = int(total)
        result["notification_issues"] = int(failed)
        return result

    def export_orders(self) -> list[dict[str, Any]]:
        columns = (
            "id", "created_at", "updated_at", "customer_name", "username", "user_id",
            "service_id", "service_title", "description", "features", "budget", "deadline",
            "repository_url", "contact", "status", "admin_notes", "notification_status",
        )
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY id ASC").fetchall()
        return [{column: row[column] for column in columns} for row in rows]

    def save_order_draft(
        self, user_id: int, state_name: str, data: dict[str, Any], *,
        chat_id: int, panel_message_id: int | None = None,
    ) -> None:
        """Persist a customer order draft so a process restart does not discard it."""
        if not state_name.startswith("OrderForm:"):
            raise ValueError("Only customer order-form states can be persisted as drafts")
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 20000:
            raise ValueError("Draft payload exceeds the supported size")
        now = utc_now()
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO order_drafts(user_id, state_name, data_json, chat_id, panel_message_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     state_name = excluded.state_name, data_json = excluded.data_json,
                     chat_id = excluded.chat_id, panel_message_id = excluded.panel_message_id,
                     updated_at = excluded.updated_at""",
                (user_id, state_name, encoded, chat_id, panel_message_id, now),
            )

    def get_order_draft(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM order_drafts WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["data"] = json.loads(result.pop("data_json"))
        except (json.JSONDecodeError, TypeError):
            return None
        return result

    def delete_order_draft(self, user_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM order_drafts WHERE user_id = ?", (user_id,))

    def set_user_ui_message(self, user_id: int, chat_id: int, message_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO user_ui_state(user_id, chat_id, panel_message_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     chat_id=excluded.chat_id,
                     panel_message_id=excluded.panel_message_id,
                     updated_at=excluded.updated_at""",
                (user_id, chat_id, message_id, utc_now()),
            )

    def get_user_ui_message(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM user_ui_state WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def clear_user_ui_message(self, user_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM user_ui_state WHERE user_id = ?", (user_id,))

    def set_admin_ui_message(self, admin_id: int, chat_id: int, message_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO admin_ui_state(admin_id, chat_id, panel_message_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(admin_id) DO UPDATE SET
                     chat_id=excluded.chat_id,
                     panel_message_id=excluded.panel_message_id,
                     updated_at=excluded.updated_at""",
                (admin_id, chat_id, message_id, utc_now()),
            )

    def get_admin_ui_message(self, admin_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM admin_ui_state WHERE admin_id = ?", (admin_id,)
            ).fetchone()
        return dict(row) if row else None

    def clear_admin_ui_message(self, admin_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM admin_ui_state WHERE admin_id = ?", (admin_id,))

    def get_customer_notification_by_message(
        self, user_id: int, message_id: int, *, statuses: tuple[str, ...] = ("sent",),
    ) -> dict[str, Any] | None:
        normalized = tuple(status for status in statuses if status in {"pending", "sent", "failed", "processed"})
        if not normalized:
            return None
        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT * FROM customer_notifications WHERE user_id = ? AND message_id = ? "
                f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (user_id, message_id, *normalized),
            ).fetchone()
        return dict(row) if row else None
