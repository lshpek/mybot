from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, BufferedInputFile,
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from dotenv import load_dotenv

from app.catalog import load_services, service_by_id
from app.keyboards import (
    admin_dashboard_menu,
    admin_notification_menu,
    admin_clarification_confirm_menu,
    admin_clarification_view_menu,
    customer_clarification_cancel_menu,
    admin_order_list_menu,
    admin_order_menu,
    budget_menu,
    confirmation_menu,
    contact_menu,
    customer_cancel_confirm,
    deadline_menu,
    edit_menu,
    features_menu,
    main_menu,
    service_catalog_menu,
    service_detail_menu,
    customer_notification_menu,
    admin_status_menu,
    price_note_menu,
    price_confirm_menu,
    my_order_actions,
    my_orders_menu,
    repository_skip_menu,
    service_menu,
    start_resume_menu,
)
from app.states import AdminNote, AdminReply, AdminSearch, AdminPrice, AdminClarification, CustomerClarification, OrderForm
from app.storage import ALLOWED_TRANSITIONS, OrderStorage

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("botvex_orders")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/botvex_orders.sqlite3").strip()
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "BOTVEX | Заказ разработки").strip()
DEVELOPER_USERNAME = os.getenv("DEVELOPER_USERNAME", "legaloz").strip().lstrip("@")
ADMIN_IDS = {
    int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit() and int(item.strip()) > 0
}
SERVICES = load_services(os.getenv("SERVICES_FILE", "config/services.json"))
GITHUB_URL = os.getenv("GITHUB_URL", "").strip()
if GITHUB_URL:
    github_parsed = urlparse(GITHUB_URL)
    if github_parsed.scheme != "https" or github_parsed.netloc.casefold() not in {"github.com", "www.github.com"} or not github_parsed.path.strip("/"):
        log.warning("GITHUB_URL is not a valid HTTPS GitHub profile/repository URL; hiding the portfolio link")
        GITHUB_URL = ""
CURRENCY = os.getenv("CURRENCY", "RUB").strip().upper() or "RUB"
SERVICES_BY_ID = {service["id"]: service for service in SERVICES}
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
try:
    DISPLAY_TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    log.warning("Unknown TIMEZONE=%s; falling back to UTC", TIMEZONE_NAME)
    TIMEZONE_NAME = "UTC"
    DISPLAY_TZ = timezone.utc
STORAGE = OrderStorage(DB_PATH)
ADMIN_PAGE_SIZE = 8
ADMIN_FILTERS = {
    "all": (None, False, "Все заявки"),
    "new": ("new", False, "Новые заявки"),
    "review": ("under_review", False, "На рассмотрении"),
    "needs_info": ("needs_info", False, "Нужно уточнение"),
    "quote": ("quote_sent", False, "Оценка подготовлена"),
    "awaiting": ("awaiting_client", False, "Ожидает решения клиента"),
    "progress": ("in_progress", False, "В работе"),
    "completed": ("completed", False, "Завершённые"),
    "cancelled": ("cancelled", False, "Отменённые"),
    "notifications": (None, True, "Проблемы уведомлений"),
}

STATUS_LABELS = {
    "new": "Новая",
    "under_review": "На рассмотрении",
    "needs_info": "Нужно уточнение",
    "quote_sent": "Оценка подготовлена",
    "awaiting_client": "Ожидает решения клиента",
    "in_progress": "В работе",
    "completed": "Завершена",
    "cancelled": "Отменена",
}
BUDGET_OPTIONS = {
    "under_5k": "До 5 000 ₽",
    "5_15k": "5 000–15 000 ₽",
    "15_30k": "15 000–30 000 ₽",
    "over_30k": "30 000 ₽ и выше",
    "skip": "Не указан",
}
DEADLINE_OPTIONS = {
    "urgent": "Срочно: 1–3 дня",
    "week": "Около недели",
    "month": "2–4 недели",
    "flexible": "Срок гибкий",
    "skip": "Не указан",
}

dp = Dispatcher(storage=MemoryStorage())


def safe(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def safe_limit(value: object, escaped_limit: int) -> str:
    """HTML-escape text while limiting the encoded output length, without cutting entities."""
    chunks: list[str] = []
    length = 0
    for char in str(value if value is not None else ""):
        encoded = html.escape(char, quote=True)
        if length + len(encoded) > escaped_limit:
            return "".join(chunks) + "…"
        chunks.append(encoded)
        length += len(encoded)
    return "".join(chunks)


def csv_safe(value: object) -> object:
    """Neutralize common spreadsheet formula prefixes in user-controlled CSV cells."""
    if not isinstance(value, str):
        return value
    candidate = value.lstrip()
    if candidate.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def format_time(value: object) -> str:
    """Format stored ISO timestamps in the configured timezone; assume UTC if naive."""
    if not value:
        return "не указано"
    try:
        raw = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y, %H:%M")
    except (ValueError, TypeError, OverflowError):
        return str(value)


def format_price(amount: object, currency: str = "RUB") -> str:
    try:
        value = Decimal(str(amount).replace(",", "."))
        rendered = f"{value:,.2f}".replace(",", " ").replace(".", ",")
        if rendered.endswith(",00"):
            rendered = rendered[:-3]
        symbol = {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get(currency.upper(), currency.upper())
        return f"{rendered} {symbol}" if symbol not in {"$", "€"} else f"{symbol}{rendered}"
    except (InvalidOperation, ValueError, TypeError):
        return f"{safe(amount)} {safe(currency)}"


def order_card(order: dict, *, admin_view: bool = False) -> str:
    display_updated = order.get("updated_at") if admin_view else order.get("customer_updated_at", order.get("updated_at"))
    text = (
        f"<b>Заявка #{order['id']}</b> · <b>{safe(STATUS_LABELS.get(order['status'], order['status']))}</b>\n"
        f"Услуга: {safe(order['service_title'])}\n"
        f"Клиент: {safe_limit(order['customer_name'], 120)}\n"
        f"Контакт: {safe_limit(order['contact'], 180)}\n"
        f"Бюджет: {safe(order['budget'])}\n"
        f"Срок: {safe(order['deadline'])}\n"
        f"Создана: {safe(format_time(order.get('created_at')))}\n"
        f"Последнее изменение: {safe(format_time(display_updated))}\n\n"
        f"<b>Задача</b>\n{safe_limit(order['description'], 700)}\n\n"
        f"<b>Желаемые функции</b>\n{safe_limit(order['features'] or 'Не указаны', 420)}"
    )
    if order.get("repository_url"):
        text += f"\n\nСсылка на проект: {safe_limit(order['repository_url'], 280)}"
    if order.get("price_amount"):
        text += f"\n\n<b>Предложенная стоимость:</b> {safe(format_price(order['price_amount'], order.get('price_currency') or 'RUB'))}"
        if order.get("price_note"):
            text += f"\n{safe_limit(order['price_note'], 400)}"
    if admin_view:
        username = f"@{safe(order['username'])}" if order.get("username") else "не указан"
        text += (
            f"\n\nПрофиль: {username}"
            f"\nTelegram ID: <code>{order['user_id']}</code>"
            f"\nУведомление админам: {safe(order['notification_status'])}"
        )
        if order.get("admin_notes"):
            text += f"\n\n<b>Внутренние заметки</b>\n{safe_limit(order['admin_notes'], 650)}"
    return text


def timeline_text(events: list[dict], *, customer_view: bool = True, max_events: int = 12) -> str:
    if customer_view:
        events = [event for event in events if int(event.get("customer_visible", 1)) == 1]
    visible_events = events[-max_events:]
    if not visible_events:
        return "<b>История</b>\nПока нет дополнительных событий."
    lines = ["<b>История заказа</b>"]
    for event in visible_events:
        actor_label = {
            "customer": "Клиент", "admin": "Разработчик", "system": "Система",
        }.get(str(event.get("actor_type", "system")), "Система")
        message = safe_limit(event.get("message", "Обновление заказа"), 260)
        lines.append(f"• <code>{safe(format_time(event.get('created_at')))}</code> — {actor_label}: {message}")
    if len(events) > max_events:
        lines.insert(1, f"<i>Показаны последние {max_events} событий.</i>")
    return "\n".join(lines)


def customer_order_text(order: dict, events: list[dict]) -> str:
    return order_card(order) + "\n\n" + timeline_text(events, customer_view=True)


def admin_order_text(order: dict, events: list[dict]) -> str:
    return order_card(order, admin_view=True) + "\n\n" + timeline_text(events, customer_view=False, max_events=12)


def home_text(latest_order: dict | None) -> str:
    text = (
        f"<b>{safe(BUSINESS_NAME)}</b>\n\n"
        "Закажи Telegram-бота, Python-автоматизацию, API-интеграцию или ИИ-решение.\n"
        "Выбери услугу и опиши задачу — я уточню требования и подготовлю оценку. "
        "Окончательная стоимость зависит от объёма работ."
    )
    if latest_order:
        status_label = STATUS_LABELS.get(latest_order["status"], latest_order["status"])
        text += (
            f"\n\n<b>Твой последний заказ: #{latest_order['id']}</b>\n"
            f"Этап: <b>{safe(status_label)}</b>\n"
            f"Обновлён: {safe(format_time(latest_order.get('customer_updated_at', latest_order.get('updated_at'))))}"
        )
    else:
        text += "\n\n<i>Пока нет оформленных заявок. Первый заказ можно создать за пару минут.</i>"
    return text


async def remember_panel(
    user_id: int, chat_id: int, message_id: int, *, keep_notification_id: int | None = None,
) -> None:
    """Persist the current customer panel and retire notification references to that message.

    When the panel changes from a notification into a normal screen, the old notification
    must no longer be eligible for cleanup later (otherwise a future navigation might
    accidentally delete the current menu/form). Delivery passes keep_notification_id to
    preserve the newly sent notification record.
    """
    await asyncio.to_thread(STORAGE.set_user_ui_message, user_id, chat_id, message_id)
    rows = await asyncio.to_thread(
        STORAGE.list_customer_notifications, user_id, limit=200, statuses=("sent",)
    )
    for row in rows:
        if int(row.get("message_id") or 0) != int(message_id):
            continue
        if keep_notification_id is not None and int(row["id"]) == keep_notification_id:
            continue
        await asyncio.to_thread(
            STORAGE.save_customer_notification_delivery, int(row["id"]), status="processed"
        )


async def remember_admin_panel(admin_id: int, chat_id: int, message_id: int) -> None:
    await asyncio.to_thread(STORAGE.set_admin_ui_message, admin_id, chat_id, message_id)


async def adopt_admin_notification_panel(query: CallbackQuery, order_id: int) -> None:
    """When an admin opens an order from its notification, reuse that message as the panel."""
    delivery = await asyncio.to_thread(
        STORAGE.get_notification_delivery, order_id, query.from_user.id
    )
    if not delivery or int(delivery.get("message_id") or 0) != query.message.message_id:
        return
    previous = await asyncio.to_thread(STORAGE.get_admin_ui_message, query.from_user.id)
    if previous and int(previous["chat_id"]) == query.message.chat.id:
        previous_id = int(previous["panel_message_id"])
        if previous_id != query.message.message_id:
            try:
                await query.bot.delete_message(query.message.chat.id, previous_id)
            except TelegramAPIError as exc:
                log.debug("Could not remove previous admin panel while opening notification (%s)", type(exc).__name__)
    await remember_admin_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await asyncio.to_thread(STORAGE.dismiss_notification, order_id, query.from_user.id)


async def render_admin_panel(
    *, admin_id: int, chat_id: int, bot: Bot, text: str, reply_markup=None,
    fallback_message: Message | None = None,
) -> int:
    """Render the administrator's interface in one tracked panel when possible."""
    previous = await asyncio.to_thread(STORAGE.get_admin_ui_message, admin_id)
    if previous and int(previous["chat_id"]) == chat_id:
        previous_id = int(previous["panel_message_id"])
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=previous_id,
                reply_markup=reply_markup,
            )
            await remember_admin_panel(admin_id, chat_id, previous_id)
            return previous_id
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                await remember_admin_panel(admin_id, chat_id, previous_id)
                return previous_id
            log.debug("Stored admin panel is not editable (%s)", type(exc).__name__)
        except TelegramAPIError as exc:
            log.debug("Stored admin panel is not editable (%s)", type(exc).__name__)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=previous_id)
        except TelegramAPIError:
            pass
    if fallback_message is not None:
        sent = await fallback_message.answer(text, reply_markup=reply_markup)
    else:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    await remember_admin_panel(admin_id, chat_id, int(sent.message_id))
    return int(sent.message_id)


async def mark_notification_message_processed(user_id: int, message_id: int) -> None:
    """Mark only the notification currently transformed into a destination panel."""
    notification = await asyncio.to_thread(
        STORAGE.get_customer_notification_by_message, user_id, message_id, statuses=("sent",)
    )
    if notification:
        await asyncio.to_thread(
            STORAGE.save_customer_notification_delivery, int(notification["id"]), status="processed"
        )


async def persist_order_draft(user_id: int, state: FSMContext, *, chat_id: int | None = None) -> None:
    state_name = await state.get_state()
    if not state_name or not state_name.startswith("OrderForm:"):
        return
    # FSMContext.key is the authoritative user identity even when this helper receives a bot-owned callback message.
    state_user_id = getattr(getattr(state, "key", None), "user_id", None)
    if state_user_id is not None:
        user_id = int(state_user_id)
    data = await state.get_data()
    panel = await asyncio.to_thread(STORAGE.get_user_ui_message, user_id)
    actual_chat_id = int(panel["chat_id"]) if panel else chat_id
    panel_message_id = int(panel["panel_message_id"]) if panel else None
    if actual_chat_id is None:
        return
    await asyncio.to_thread(
        STORAGE.save_order_draft, user_id, state_name, data,
        chat_id=actual_chat_id, panel_message_id=panel_message_id,
    )


async def delete_user_message(message: Message) -> bool:
    """Best-effort deletion of an already processed user message."""
    try:
        await message.delete()
        return True
    except TelegramAPIError as exc:
        log.debug("Could not delete processed user message (%s)", type(exc).__name__)
        return False


class PrivateMessageCleanupMiddleware(BaseMiddleware):
    """Best-effort deletion of incoming private-chat messages after processing succeeds."""
    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        if isinstance(event, Message) and event.chat.type == ChatType.PRIVATE and event.from_user:
            await delete_user_message(event)
        return result


dp.message.outer_middleware(PrivateMessageCleanupMiddleware())


async def answer_panel(message: Message, text: str, reply_markup=None) -> Message:
    """Edit the currently tracked bot panel for text-driven form steps when possible."""
    if message.chat.type == ChatType.PRIVATE and message.from_user:
        previous = await asyncio.to_thread(STORAGE.get_user_ui_message, message.from_user.id)
        if previous and int(previous["chat_id"]) == message.chat.id:
            panel_id = int(previous["panel_message_id"])
            if panel_id != message.message_id:
                try:
                    edited = await message.bot.edit_message_text(
                        text=text, chat_id=message.chat.id, message_id=panel_id,
                        reply_markup=reply_markup,
                    )
                    await remember_panel(message.from_user.id, message.chat.id, panel_id)
                    return edited
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).casefold():
                        await remember_panel(message.from_user.id, message.chat.id, panel_id)
                        return message
                    log.debug("Could not edit tracked panel; creating replacement (%s)", type(exc).__name__)
                except TelegramAPIError as exc:
                    log.debug("Could not edit tracked panel; creating replacement (%s)", type(exc).__name__)
                try:
                    await message.bot.delete_message(message.chat.id, panel_id)
                except TelegramAPIError:
                    pass
    sent = await message.answer(text, reply_markup=reply_markup)
    if message.chat.type == ChatType.PRIVATE and message.from_user:
        await remember_panel(message.from_user.id, message.chat.id, sent.message_id)
    return sent


async def edit_panel_by_state(bot: Bot, data: dict, text: str, reply_markup=None) -> bool:
    chat_id = data.get("admin_panel_chat_id")
    message_id = data.get("admin_panel_message_id")
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_text(text, chat_id=int(chat_id), message_id=int(message_id), reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        return "message is not modified" in str(exc).casefold()
    except TelegramAPIError as exc:
        log.warning("Could not edit admin action panel (%s)", type(exc).__name__)
        return False


async def clear_customer_notifications(
    bot: Bot, user_id: int, *, order_id: int | None = None,
    exclude_message_id: int | None = None,
) -> None:
    """Remove tracked notifications; only mark cleanup complete when it succeeded or the panel is reused."""
    rows = await asyncio.to_thread(
        STORAGE.list_customer_notifications, user_id, order_id=order_id, limit=100, statuses=("sent",)
    )
    for row in rows:
        message_id = row.get("message_id")
        if not message_id:
            continue
        notification_id = int(row["id"])
        if exclude_message_id is not None and int(message_id) == exclude_message_id:
            # Caller is transforming the same message into a destination screen. Mark it
            # processed only after that screen was successfully rendered.
            continue
        removed = False
        try:
            await bot.delete_message(chat_id=user_id, message_id=int(message_id))
            removed = True
        except TelegramAPIError as exc:
            log.debug("Could not remove customer notification %s (%s)", notification_id, type(exc).__name__)
            try:
                await bot.edit_message_reply_markup(chat_id=user_id, message_id=int(message_id), reply_markup=None)
                removed = True
            except TelegramAPIError as markup_exc:
                log.debug("Could not deactivate customer notification %s (%s)", notification_id, type(markup_exc).__name__)
        if removed:
            await asyncio.to_thread(STORAGE.save_customer_notification_delivery, notification_id, status="processed")


async def deliver_customer_notification(bot: Bot, notification_id: int) -> bool:
    """Replace the tracked customer panel with a notification where possible, then persist delivery."""
    row = await asyncio.to_thread(STORAGE.get_customer_notification, notification_id)
    if not row or row.get("status") in {"sent", "processed"}:
        return bool(row and row.get("status") in {"sent", "processed"})
    order_id = int(row["order_id"])
    user_id = int(row["user_id"])
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, user_id)
    if not order:
        await asyncio.to_thread(
            STORAGE.save_customer_notification_delivery, notification_id,
            status="failed", error="Order ownership check failed",
        )
        return False

    previous = await asyncio.to_thread(STORAGE.get_user_ui_message, user_id)
    previous_chat_id = int(previous["chat_id"]) if previous else user_id
    previous_message_id = int(previous["panel_message_id"]) if previous else None
    active_clarification = None
    if row.get("notification_type") == "clarification_requested":
        active_clarification = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    markup = customer_notification_menu(
        order_id, notification_id,
        can_reply_to_clarification=bool(active_clarification),
    )
    message_id: int | None = None
    reused_panel = False
    try:
        if previous_message_id is not None:
            try:
                await bot.edit_message_text(
                    text=row["body"], chat_id=previous_chat_id,
                    message_id=previous_message_id, reply_markup=markup,
                )
                message_id = previous_message_id
                reused_panel = True
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    message_id = previous_message_id
                    reused_panel = True
                else:
                    log.debug("Could not replace active panel with notification (%s)", type(exc).__name__)
        if message_id is None:
            sent = await bot.send_message(user_id, row["body"], reply_markup=markup)
            message_id = int(sent.message_id)
    except TelegramAPIError as exc:
        await asyncio.to_thread(
            STORAGE.save_customer_notification_delivery, notification_id,
            status="failed", error=type(exc).__name__,
        )
        log.warning("Customer notification %s delivery failed (%s)", notification_id, type(exc).__name__)
        return False

    await asyncio.to_thread(
        STORAGE.save_customer_notification_delivery, notification_id,
        status="sent", message_id=message_id,
    )
    if previous_message_id is not None and not reused_panel and previous_message_id != message_id:
        try:
            await bot.delete_message(chat_id=previous_chat_id, message_id=previous_message_id)
        except TelegramAPIError as exc:
            log.debug("Could not delete previous panel after notification delivery (%s)", type(exc).__name__)
            try:
                await bot.edit_message_text(
                    "<b>BOTVEX</b>\n\nНовое уведомление отправлено отдельным сообщением.",
                    chat_id=previous_chat_id, message_id=previous_message_id, reply_markup=None,
                )
            except TelegramAPIError:
                pass

    # If a reused message ID now represents this newest notification, retire old references to it.
    old_notifications = await asyncio.to_thread(
        STORAGE.list_customer_notifications, user_id, limit=100, statuses=("sent",)
    )
    for old in old_notifications:
        if int(old["id"]) != notification_id and int(old.get("message_id") or 0) == message_id:
            await asyncio.to_thread(STORAGE.save_customer_notification_delivery, int(old["id"]), status="processed")
    await remember_panel(user_id, previous_chat_id, message_id, keep_notification_id=notification_id)
    return True


async def queue_customer_notification(
    bot: Bot, *, order_id: int, user_id: int, notification_type: str,
    event_key: str, body: str,
) -> bool:
    notification_id = await asyncio.to_thread(
        STORAGE.create_customer_notification, event_key=event_key, order_id=order_id,
        user_id=user_id, notification_type=notification_type, body=body,
    )
    return await deliver_customer_notification(bot, notification_id)


async def show_home(
    *, user_id: int, chat_id: int, bot: Bot, target_message: Message | None = None,
) -> None:
    latest = await asyncio.to_thread(STORAGE.get_latest_user_order, user_id)
    text = home_text(latest)
    markup = main_menu(int(latest["id"]) if latest else None)
    if target_message is not None:
        try:
            await target_message.edit_text(text, reply_markup=markup)
            await remember_panel(user_id, chat_id, target_message.message_id)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                await remember_panel(user_id, chat_id, target_message.message_id)
                return
            log.debug("Could not edit current user panel; sending a replacement (%s)", type(exc).__name__)
        except TelegramAPIError as exc:
            log.debug("Could not edit current user panel; sending a replacement (%s)", type(exc).__name__)
        try:
            await target_message.delete()
        except TelegramAPIError as delete_exc:
            log.debug("Could not remove replaced user panel (%s)", type(delete_exc).__name__)
    else:
        previous = await asyncio.to_thread(STORAGE.get_user_ui_message, user_id)
        if previous and int(previous["chat_id"]) == chat_id:
            try:
                await bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=int(previous["panel_message_id"]),
                    reply_markup=markup,
                )
                await remember_panel(user_id, chat_id, int(previous["panel_message_id"]))
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    await remember_panel(user_id, chat_id, int(previous["panel_message_id"]))
                    return
                log.debug("Stored user panel is no longer editable; sending a replacement (%s)", type(exc).__name__)
            except TelegramAPIError as exc:
                log.debug("Stored user panel is no longer editable; sending a replacement (%s)", type(exc).__name__)
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await remember_panel(user_id, chat_id, sent.message_id)


def review_text(data: dict) -> str:
    service = str(data.get("service_title", "Не выбрана"))
    repo = data.get("repository_url") or "Не указана"
    features = data.get("features") or "Не указаны"
    return (
        "<b>Проверь заявку перед отправкой</b>\n\n"
        f"<b>Услуга:</b> {safe(service)}\n"
        f"<b>Описание:</b>\n{safe_limit(data.get('description', ''), 1000)}\n\n"
        f"<b>Функции:</b>\n{safe_limit(features, 600)}\n\n"
        f"<b>Бюджет:</b> {safe_limit(data.get('budget', 'Не указан'), 150)}\n"
        f"<b>Срок:</b> {safe_limit(data.get('deadline', 'Не указан'), 150)}\n"
        f"<b>Ссылка:</b> {safe_limit(repo, 280)}\n"
        f"<b>Связь:</b> {safe_limit(data.get('contact', 'Не выбрана'), 180)}\n\n"
        "Если всё верно, отправь заявку. Окончательная стоимость определяется после уточнения требований."
    )


async def show_review(message: Message, state: FSMContext, *, edit_existing: bool = False) -> None:
    data = await state.get_data()
    await state.update_data(editing=False)
    await state.set_state(OrderForm.confirm)
    text = review_text(data)
    if edit_existing:
        await message.edit_text(text, reply_markup=confirmation_menu())
        if message.from_user:
            await remember_panel(message.from_user.id, message.chat.id, message.message_id)
    else:
        await answer_panel(message, text, reply_markup=confirmation_menu())
    if message.from_user:
        await persist_order_draft(message.from_user.id, state, chat_id=message.chat.id)


async def move_to_budget(message: Message, state: FSMContext, *, edit_existing: bool = False) -> None:
    await state.set_state(OrderForm.budget)
    text = "Какой бюджет ты рассматриваешь? Это ориентир, а не окончательная цена."
    if edit_existing:
        await message.edit_text(text, reply_markup=budget_menu())
        if message.from_user:
            await remember_panel(message.from_user.id, message.chat.id, message.message_id)
    else:
        await answer_panel(message, text, reply_markup=budget_menu())
    if message.from_user:
        await persist_order_draft(message.from_user.id, state, chat_id=message.chat.id)


async def move_to_deadline(message: Message, state: FSMContext, *, edit_existing: bool = False) -> None:
    await state.set_state(OrderForm.deadline)
    text = "К какому сроку желательно подготовить решение?"
    if edit_existing:
        await message.edit_text(text, reply_markup=deadline_menu())
        if message.from_user:
            await remember_panel(message.from_user.id, message.chat.id, message.message_id)
    else:
        await answer_panel(message, text, reply_markup=deadline_menu())
    if message.from_user:
        await persist_order_draft(message.from_user.id, state, chat_id=message.chat.id)


async def move_to_repository(message: Message, state: FSMContext, *, edit_existing: bool = False) -> None:
    await state.set_state(OrderForm.repository_url)
    text = "Есть GitHub-репозиторий или ссылка на существующий проект? Отправь полную ссылку или пропусти шаг."
    if edit_existing:
        await message.edit_text(text, reply_markup=repository_skip_menu())
        if message.from_user:
            await remember_panel(message.from_user.id, message.chat.id, message.message_id)
    else:
        await answer_panel(message, text, reply_markup=repository_skip_menu())
    if message.from_user:
        await persist_order_draft(message.from_user.id, state, chat_id=message.chat.id)


async def move_to_contact(message: Message, state: FSMContext, *, edit_existing: bool = False) -> None:
    await state.set_state(OrderForm.contact_choice)
    text = "Как удобнее связаться по этой заявке?"
    if edit_existing:
        await message.edit_text(text, reply_markup=contact_menu())
        if message.from_user:
            await remember_panel(message.from_user.id, message.chat.id, message.message_id)
    else:
        await answer_panel(message, text, reply_markup=contact_menu())
    if message.from_user:
        await persist_order_draft(message.from_user.id, state, chat_id=message.chat.id)


async def finish_or_review(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("editing"):
        await show_review(message, state)
    else:
        await move_to_budget(message, state)


async def notify_admins(bot: Bot, order_id: int) -> bool:
    """Deliver one durable notification per configured administrator without duplicates."""
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        return False
    if not ADMIN_IDS:
        log.error("Order #%s saved but ADMIN_IDS is empty; notification remains pending", order_id)
        return False

    for admin_id in sorted(ADMIN_IDS):
        previous = await asyncio.to_thread(STORAGE.get_notification_delivery, order_id, admin_id)
        # A sent notification is not re-created merely because another admin failed.
        if previous and previous.get("status") == "sent" and previous.get("message_id"):
            continue
        try:
            sent = await bot.send_message(
                admin_id,
                order_card(order, admin_view=True),
                reply_markup=admin_notification_menu(order_id),
            )
            await asyncio.to_thread(
                STORAGE.save_notification_delivery, order_id, admin_id, admin_id,
                status="sent", message_id=sent.message_id,
            )
        except TelegramAPIError as exc:
            # Do not include message contents, tokens or user data in the error field.
            safe_error = type(exc).__name__
            await asyncio.to_thread(
                STORAGE.save_notification_delivery, order_id, admin_id, admin_id,
                status="failed", error=safe_error,
            )
            log.warning("Failed to deliver notification for order #%s to admin ID %s (%s)",
                        order_id, admin_id, safe_error)

    deliveries = []
    for admin_id in sorted(ADMIN_IDS):
        delivery = await asyncio.to_thread(STORAGE.get_notification_delivery, order_id, admin_id)
        deliveries.append(bool(delivery and delivery.get("status") == "sent"))
    all_sent = bool(deliveries) and all(deliveries)
    await asyncio.to_thread(STORAGE.set_notification_status, order_id, "sent" if all_sent else "failed")
    return all_sent


def order_state_screen(current_state: str | None, data: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Recreate the last form screen when the user explicitly resumes a draft."""
    state_key = (current_state or "").split(":", 1)[-1]
    screens: dict[str, tuple[str, InlineKeyboardMarkup | None]] = {
        "service": ("<b>Шаг 1 · Выбери услугу</b>", service_menu(SERVICES)),
        "description": ("<b>Шаг 2 · Опиши задачу</b>\nЧто должно делать решение? (5–1500 символов)", None),
        "features": ("<b>Шаг 3 · Какие функции нужны?</b>\nПеречисли требования или пропусти шаг.", features_menu()),
        "budget": ("Какой бюджет ты рассматриваешь? Это ориентир, а не окончательная цена.", budget_menu()),
        "budget_custom": ("Укажи примерный бюджет текстом (до 100 символов).", None),
        "deadline": ("К какому сроку желательно подготовить решение?", deadline_menu()),
        "repository_url": ("Есть ссылка на существующий проект? Отправь её или пропусти шаг.", repository_skip_menu()),
        "contact_choice": ("Как удобнее связаться по этой заявке?", contact_menu()),
        "custom_contact": ("Укажи телефон, email или другой удобный контакт (5–150 символов).", None),
        "confirm": (review_text(data), confirmation_menu()),
    }
    return screens.get(state_key, ("Не удалось восстановить шаг. Можно начать новую заявку.", main_menu()))


@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    """Reset the visible navigation on /start; fall back to editing if deletion is forbidden."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    current_state = await state.get_state()
    if not current_state:
        saved_draft = await asyncio.to_thread(STORAGE.get_order_draft, user_id)
        if saved_draft and str(saved_draft.get("state_name", "")).startswith("OrderForm:"):
            try:
                await state.set_state(str(saved_draft["state_name"]))
                await state.set_data(saved_draft.get("data", {}))
                current_state = str(saved_draft["state_name"])
            except (KeyError, TypeError, ValueError):
                await asyncio.to_thread(STORAGE.delete_order_draft, user_id)
                current_state = None

    has_draft = bool(current_state and current_state.startswith("OrderForm:"))
    if not has_draft:
        await state.clear()
        current_state = None

    latest = await asyncio.to_thread(STORAGE.get_latest_user_order, user_id)
    text = (
        "<b>Незавершённая заявка</b>\n\n"
        "Черновик хранится отдельно от отправленных заявок. Можно продолжить с последнего шага, начать заново "
        "или удалить только черновик. Уже отправленные заявки останутся без изменений."
        if has_draft else home_text(latest)
    )
    markup = start_resume_menu() if has_draft else main_menu(int(latest["id"]) if latest else None)
    previous = await asyncio.to_thread(STORAGE.get_user_ui_message, user_id)
    await clear_customer_notifications(bot, user_id)

    if previous and int(previous["chat_id"]) == chat_id:
        previous_id = int(previous["panel_message_id"])
        try:
            await bot.delete_message(chat_id=chat_id, message_id=previous_id)
        except TelegramAPIError as exc:
            # Telegram does not permit deleting every old message in every circumstance.
            # If deletion is denied but editing is possible, reuse that message to avoid stacking menus.
            log.debug("Could not delete previous /start panel (%s); trying in-place replacement", type(exc).__name__)
            try:
                await bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=previous_id, reply_markup=markup,
                )
                await remember_panel(user_id, chat_id, previous_id)
                if has_draft:
                    await persist_order_draft(user_id, state, chat_id=chat_id)
                return
            except TelegramAPIError as edit_exc:
                if "message is not modified" in str(edit_exc).casefold():
                    await remember_panel(user_id, chat_id, previous_id)
                    if has_draft:
                        await persist_order_draft(user_id, state, chat_id=chat_id)
                    return
                log.debug("Could not reuse previous /start panel (%s)", type(edit_exc).__name__)

    await asyncio.to_thread(STORAGE.clear_user_ui_message, user_id)
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await remember_panel(user_id, chat_id, sent.message_id)
    if has_draft:
        await persist_order_draft(user_id, state, chat_id=chat_id)


@dp.callback_query(F.data == "start:resume", F.message.chat.type == ChatType.PRIVATE)
async def resume_order_draft(query: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if not current_state or not current_state.startswith("OrderForm:"):
        await query.answer("Черновик не найден. Начни новую заявку.", show_alert=True)
        await show_home(user_id=query.from_user.id, chat_id=query.message.chat.id,
                        bot=query.bot, target_message=query.message)
        return
    text, markup = order_state_screen(current_state, await state.get_data())
    await query.message.edit_text(text, reply_markup=markup)
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer("Продолжаем заявку")


@dp.callback_query(F.data == "start:restart", F.message.chat.type == ChatType.PRIVATE)
async def restart_order_draft(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(STORAGE.delete_order_draft, query.from_user.id)
    await state.update_data(editing=False, submission_key=uuid4().hex)
    await state.set_state(OrderForm.service)
    await query.message.edit_text("<b>Новая заявка · Шаг 1</b>\nВыбери услугу:", reply_markup=service_menu(SERVICES))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer("Начинаем заново")


@dp.callback_query(F.data == "start:discard", F.message.chat.type == ChatType.PRIVATE)
async def discard_order_draft(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(STORAGE.delete_order_draft, query.from_user.id)
    await show_home(user_id=query.from_user.id, chat_id=query.message.chat.id,
                    bot=query.bot, target_message=query.message)
    await query.answer("Черновик удалён; сохранённые заказы на месте")


@dp.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cancel_command(message: Message, state: FSMContext, bot: Bot) -> None:
    current_state = await state.get_state()
    state_data = await state.get_data()
    await state.clear()
    if current_state and current_state.startswith("OrderForm:"):
        await asyncio.to_thread(STORAGE.delete_order_draft, message.from_user.id)
    if message.from_user.id in ADMIN_IDS and current_state and current_state.startswith(("AdminNote:", "AdminReply:", "AdminSearch:", "AdminPrice:", "AdminClarification:")):
        order_id = int(state_data.get("admin_order_id", 0) or 0)
        order = await asyncio.to_thread(STORAGE.get_order, order_id) if order_id else None
        if order:
            events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
            text = admin_order_text(order, events)
            markup = admin_order_menu(order_id, order["status"])
        else:
            text = admin_dashboard_text(await asyncio.to_thread(STORAGE.get_stats))
            markup = admin_dashboard_menu()
        await render_admin_panel(
            admin_id=message.from_user.id, chat_id=message.chat.id, bot=bot,
            text="<b>Действие отменено.</b>\n\n" + text, reply_markup=markup,
        )
        return
    await show_home(user_id=message.from_user.id, chat_id=message.chat.id, bot=bot)


def admin_dashboard_text(stats: dict[str, int]) -> str:
    return (
        "<b>🛠 Панель администратора BOTVEX</b>\n\n"
        f"🆕 Новые: <b>{stats.get('new', 0)}</b>\n"
        f"🔎 На рассмотрении: <b>{stats.get('under_review', 0)}</b>\n"
        f"💬 Нужно уточнение: <b>{stats.get('needs_info', 0)}</b>\n"
        f"💰 Оценка подготовлена: <b>{stats.get('quote_sent', 0)}</b>\n"
        f"⏳ Ожидают решения клиента: <b>{stats.get('awaiting_client', 0)}</b>\n"
        f"⚙️ В работе: <b>{stats.get('in_progress', 0)}</b>\n"
        f"✅ Завершённые: <b>{stats.get('completed', 0)}</b>\n"
        f"🚫 Отменённые: <b>{stats.get('cancelled', 0)}</b>\n"
        f"📨 Проблемы доставки уведомлений: <b>{stats.get('notification_issues', 0)}</b>\n"
        f"\nВсего заявок: <b>{stats.get('total', 0)}</b>\n"
        "Выбери нужный раздел. Заявки хранятся в базе данных независимо от уведомлений."
    )


async def send_admin_dashboard(message: Message) -> None:
    stats = await asyncio.to_thread(STORAGE.get_stats)
    await render_admin_panel(
        admin_id=message.from_user.id, chat_id=message.chat.id, bot=message.bot,
        text=admin_dashboard_text(stats), reply_markup=admin_dashboard_menu(),
        fallback_message=message,
    )


async def edit_admin_dashboard(query: CallbackQuery) -> None:
    stats = await asyncio.to_thread(STORAGE.get_stats)
    try:
        await query.message.edit_text(admin_dashboard_text(stats), reply_markup=admin_dashboard_menu())
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise
    await remember_admin_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


async def show_admin_order_list(
    *, target: Message, filter_key: str = "all", page: int = 0, edit_existing: bool = False,
    admin_id: int | None = None,
) -> None:
    if filter_key not in ADMIN_FILTERS:
        filter_key = "all"
    status, notifications_only, title = ADMIN_FILTERS[filter_key]
    safe_page = max(0, int(page))
    total = await asyncio.to_thread(STORAGE.count_orders, status, notifications_only)
    found = await asyncio.to_thread(
        STORAGE.list_orders, ADMIN_PAGE_SIZE + 1, safe_page * ADMIN_PAGE_SIZE,
        status, notifications_only,
    )
    has_next = len(found) > ADMIN_PAGE_SIZE
    orders = found[:ADMIN_PAGE_SIZE]
    if orders:
        text = f"<b>📋 {safe(title)}</b>\nВсего: {total} · Страница {safe_page + 1}\n\nВыбери заявку:"
    else:
        text = f"<b>📋 {safe(title)}</b>\n\nВ этом разделе пока нет заявок."
    markup = admin_order_list_menu(orders, filter_key=filter_key, page=safe_page, has_next=has_next)
    if edit_existing:
        try:
            await target.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                raise
        if admin_id is not None:
            await remember_admin_panel(admin_id, target.chat.id, target.message_id)
    else:
        resolved_admin_id = admin_id or (target.from_user.id if target.from_user else None)
        if resolved_admin_id is None:
            sent = await target.answer(text, reply_markup=markup)
            return
        await render_admin_panel(
            admin_id=resolved_admin_id, chat_id=target.chat.id, bot=target.bot,
            text=text, reply_markup=markup, fallback_message=target,
        )


@dp.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def admin_command(message: Message) -> None:
    # Unauthorized /admin must be silent. PrivateMessageCleanupMiddleware removes the input.
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await send_admin_dashboard(message)


@dp.callback_query(F.data == "admin:dashboard", F.message.chat.type == ChatType.PRIVATE)
async def admin_dashboard_callback(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    await edit_admin_dashboard(query)


@dp.callback_query(F.data.startswith("admin:list:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_list_callback(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, filter_key, raw_page = query.data.split(":", 3)
        page = int(raw_page)
        if filter_key not in ADMIN_FILTERS or page < 0:
            raise ValueError
    except ValueError:
        await query.answer("Некорректный фильтр или номер страницы", show_alert=True)
        return
    await show_admin_order_list(target=query.message, filter_key=filter_key, page=page, edit_existing=True, admin_id=query.from_user.id)
    await query.answer()


@dp.callback_query(F.data == "admin:search", F.message.chat.type == ChatType.PRIVATE)
async def admin_search_start(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminSearch.order_id)
    await state.update_data(
        admin_panel_chat_id=query.message.chat.id,
        admin_panel_message_id=query.message.message_id,
    )
    await query.message.edit_text("<b>Поиск заявки</b>\n\nВведи числовой номер заявки, например 1042. Для отмены отправь /cancel.")
    await remember_admin_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.message(AdminSearch.order_id, F.text, F.chat.type == ChatType.PRIVATE)
async def admin_search_order(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data = await state.get_data()
    value = message.text.strip().lstrip("#")
    if not value.isdigit():
        await edit_panel_by_state(
            bot, data,
            "<b>Поиск заявки</b>\n\nНужен числовой ID, например 1042. Отправь номер ещё раз или /cancel для отмены.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Панель", callback_data="admin:dashboard")]]),
        )
        return
    order = await asyncio.to_thread(STORAGE.get_order, int(value))
    await state.clear()
    if not order:
        await edit_panel_by_state(
            bot, data, "<b>Поиск заявки</b>\n\nЗаявка не найдена. Введи другой ID или вернись в панель.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Панель", callback_data="admin:dashboard")]]),
        )
        return
    events = await asyncio.to_thread(STORAGE.list_order_events, int(order["id"]), customer_visible_only=False, limit=250)
    await edit_panel_by_state(
        bot, data, admin_order_text(order, events),
        admin_order_menu(int(order["id"]), str(order["status"])),
    )
    await remember_admin_panel(message.from_user.id, int(data.get("admin_panel_chat_id") or message.chat.id), int(data.get("admin_panel_message_id") or 0))


@dp.message(Command("orders"), F.chat.type == ChatType.PRIVATE)
async def admin_orders_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await show_admin_order_list(target=message, filter_key="all", page=0, admin_id=message.from_user.id)


@dp.message(Command("stats"), F.chat.type == ChatType.PRIVATE)
async def admin_stats_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    stats = await asyncio.to_thread(STORAGE.get_stats)
    lines = [f"Всего заявок: <b>{stats.get('total', 0)}</b>"]
    for status, label in STATUS_LABELS.items():
        if stats.get(status, 0):
            lines.append(f"{safe(label)}: {stats[status]}")
    await render_admin_panel(
        admin_id=message.from_user.id, chat_id=message.chat.id, bot=message.bot,
        text="📊 <b>Статистика BOTVEX</b>\n\n" + "\n".join(lines),
        reply_markup=admin_dashboard_menu(), fallback_message=message,
    )


@dp.message(Command("export"), F.chat.type == ChatType.PRIVATE)
async def admin_export_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    rows = await asyncio.to_thread(STORAGE.export_orders)
    if not rows:
        await render_admin_panel(
            admin_id=message.from_user.id, chat_id=message.chat.id, bot=message.bot,
            text="В базе пока нет заявок для экспорта.", reply_markup=admin_dashboard_menu(),
            fallback_message=message,
        )
        return
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: csv_safe(value) for key, value in row.items()})
    payload = buffer.getvalue().encode("utf-8-sig")
    await message.answer_document(BufferedInputFile(payload, filename="botvex_orders.csv"), caption="Экспорт заявок BOTVEX")


@dp.message(Command("retry_notifications"), F.chat.type == ChatType.PRIVATE)
async def retry_notifications_command(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    sent, attempted = await retry_failed_notifications(bot, limit=50)
    stats = await asyncio.to_thread(STORAGE.get_stats)
    await render_admin_panel(
        admin_id=message.from_user.id, chat_id=message.chat.id, bot=bot,
        text=admin_dashboard_text(stats) + f"\n\nПовтор доставки: успешно {sent} из {attempted}.",
        reply_markup=admin_dashboard_menu(), fallback_message=message,
    )


@dp.callback_query(F.data == "order:new", F.message.chat.type == ChatType.PRIVATE)
async def start_order(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(STORAGE.delete_order_draft, query.from_user.id)
    await state.update_data(editing=False, submission_key=uuid4().hex)
    await state.set_state(OrderForm.service)
    await query.message.edit_text("<b>Шаг 1/7 · Выбери услугу</b>", reply_markup=service_menu(SERVICES))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data.startswith("service:"), OrderForm.service)
async def choose_service(query: CallbackQuery, state: FSMContext) -> None:
    service_id = query.data.split(":", 1)[1]
    service = service_by_id(SERVICES, service_id)
    if not service:
        await query.answer("Услуга не найдена. Обнови меню через /start.", show_alert=True)
        return
    await state.update_data(service_id=service["id"], service_title=service["title"])
    data = await state.get_data()
    if data.get("editing"):
        await show_review(query.message, state, edit_existing=True)
    else:
        await state.set_state(OrderForm.description)
        await query.message.edit_text(
            f"Выбрано: <b>{safe(service['title'])}</b>\n\n"
            "<b>Шаг 2/7 · Опиши задачу</b>\n"
            "Что должно делать решение и какую проблему оно должно решить? (5–1500 символов)",
        )
        await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
        await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.message(OrderForm.description, F.text, F.chat.type == ChatType.PRIVATE)
async def get_description(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    if len(value) < 5 or len(value) > 1500:
        await answer_panel(message, "<b>Шаг 2/7 · Опиши задачу</b>\nОписание должно содержать от 5 до 1500 символов. Попробуй ещё раз.")
        return
    await state.update_data(description=value)
    data = await state.get_data()
    if data.get("editing"):
        await show_review(message, state)
        return
    await state.set_state(OrderForm.features)
    await answer_panel(
        message,
        "<b>Шаг 3/7 · Какие функции нужны?</b>\n"
        "Перечисли кнопки, команды, интеграции или другие требования (до 1000 символов).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="skip:features")],
            [InlineKeyboardButton(text="❌ Отменить заявку", callback_data="order:cancel", style="danger")],
        ]),
    )
    await persist_order_draft(message.from_user.id, state, chat_id=message.chat.id)


@dp.message(OrderForm.features, F.text, F.chat.type == ChatType.PRIVATE)
async def get_features(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    if len(value) > 1000:
        await answer_panel(message, "<b>Шаг 3/7 · Какие функции нужны?</b>\nСписок функций слишком длинный. Ограничь его 1000 символами.", reply_markup=features_menu())
        return
    await state.update_data(features=value or "Не указаны")
    data = await state.get_data()
    if data.get("editing"):
        await show_review(message, state)
    else:
        await move_to_budget(message, state)


@dp.callback_query(F.data == "skip:features", OrderForm.features)
async def skip_features(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(features="Не указаны")
    data = await state.get_data()
    if data.get("editing"):
        await show_review(query.message, state, edit_existing=True)
    else:
        await move_to_budget(query.message, state, edit_existing=True)
    await query.answer()


@dp.callback_query(F.data.startswith("budget:"), OrderForm.budget)
async def choose_budget(query: CallbackQuery, state: FSMContext) -> None:
    code = query.data.split(":", 1)[1]
    if code == "custom":
        await state.set_state(OrderForm.budget_custom)
        await query.message.edit_text("Укажи примерный бюджет текстом (до 100 символов). Можно написать «не знаю».")
        await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
        await query.answer()
        return
    if code not in BUDGET_OPTIONS:
        await query.answer("Неизвестный вариант бюджета", show_alert=True)
        return
    await state.update_data(budget=BUDGET_OPTIONS[code])
    data = await state.get_data()
    if data.get("editing"):
        await show_review(query.message, state, edit_existing=True)
    else:
        await move_to_deadline(query.message, state, edit_existing=True)
    await query.answer()


@dp.message(OrderForm.budget_custom, F.text, F.chat.type == ChatType.PRIVATE)
async def get_custom_budget(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    if len(value) < 2 or len(value) > 100:
        await answer_panel(message, "Укажи примерный бюджет длиной от 2 до 100 символов. Можно написать «не знаю».")
        return
    await state.update_data(budget=value)
    data = await state.get_data()
    if data.get("editing"):
        await show_review(message, state)
    else:
        await move_to_deadline(message, state)


@dp.callback_query(F.data.startswith("deadline:"), OrderForm.deadline)
async def choose_deadline(query: CallbackQuery, state: FSMContext) -> None:
    code = query.data.split(":", 1)[1]
    if code not in DEADLINE_OPTIONS:
        await query.answer("Неизвестный вариант срока", show_alert=True)
        return
    await state.update_data(deadline=DEADLINE_OPTIONS[code])
    data = await state.get_data()
    if data.get("editing"):
        await show_review(query.message, state, edit_existing=True)
    else:
        await move_to_repository(query.message, state, edit_existing=True)
    await query.answer()


@dp.message(OrderForm.repository_url, F.text, F.chat.type == ChatType.PRIVATE)
async def get_repository_url(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    if value.casefold() in {"/skip", "пропустить", "нет"}:
        value = ""
    elif len(value) > 300:
        await answer_panel(message, "Ссылка слишком длинная (максимум 300 символов). Пришли ссылку ещё раз или выбери пропуск.", reply_markup=repository_skip_menu())
        return
    else:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            await answer_panel(message, "Пришли полную ссылку, начинающуюся с https:// или http://, либо нажми «Пропустить».", reply_markup=repository_skip_menu())
            return
    await state.update_data(repository_url=value)
    data = await state.get_data()
    if data.get("editing"):
        await show_review(message, state)
    else:
        await move_to_contact(message, state)


@dp.callback_query(F.data == "repo:skip", OrderForm.repository_url)
async def skip_repository(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(repository_url="")
    data = await state.get_data()
    if data.get("editing"):
        await show_review(query.message, state, edit_existing=True)
    else:
        await move_to_contact(query.message, state, edit_existing=True)
    await query.answer()


@dp.callback_query(F.data == "contact:telegram", OrderForm.contact_choice)
async def choose_telegram_contact(query: CallbackQuery, state: FSMContext) -> None:
    username = query.from_user.username
    contact = f"Telegram: @{username}" if username else "Связь через этот Telegram-бот"
    await state.update_data(contact=contact)
    await show_review(query.message, state, edit_existing=True)
    await query.answer()


@dp.callback_query(F.data == "contact:custom", OrderForm.contact_choice)
async def choose_custom_contact(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OrderForm.custom_contact)
    await query.message.edit_text("Укажи телефон, email или другой удобный контакт (5–150 символов).")
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.message(OrderForm.custom_contact, F.text, F.chat.type == ChatType.PRIVATE)
async def get_custom_contact(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    if len(value) < 5 or len(value) > 150:
        await answer_panel(message, "Контакт должен содержать от 5 до 150 символов. Укажи телефон, email или другой удобный контакт.")
        return
    await state.update_data(contact=value)
    await show_review(message, state)


@dp.callback_query(F.data == "edit:menu", OrderForm.confirm)
async def open_edit_menu(query: CallbackQuery) -> None:
    await query.message.edit_text("Что нужно исправить?", reply_markup=edit_menu())
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data == "edit:back", OrderForm.confirm)
async def return_to_review(query: CallbackQuery, state: FSMContext) -> None:
    await show_review(query.message, state, edit_existing=True)
    await query.answer()


@dp.callback_query(F.data == "edit:service", OrderForm.confirm)
async def edit_service(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await state.set_state(OrderForm.service)
    await query.message.edit_text("Выбери другую услугу:", reply_markup=service_menu(SERVICES))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data == "edit:description", OrderForm.confirm)
async def edit_description(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await state.set_state(OrderForm.description)
    await query.message.edit_text("Отправь новое описание задачи (5–1500 символов).")
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data == "edit:features", OrderForm.confirm)
async def edit_features(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await state.set_state(OrderForm.features)
    await query.message.edit_text("Отправь новый список функций (до 1000 символов) или нажми «Пропустить».", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="skip:features")],
    ]))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data == "edit:budget", OrderForm.confirm)
async def edit_budget(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await state.set_state(OrderForm.budget)
    await query.message.edit_text("Выбери бюджет:", reply_markup=budget_menu())
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data == "edit:deadline", OrderForm.confirm)
async def edit_deadline(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await state.set_state(OrderForm.deadline)
    await query.message.edit_text("Выбери срок:", reply_markup=deadline_menu())
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data == "edit:repo", OrderForm.confirm)
async def edit_repository(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await move_to_repository(query.message, state, edit_existing=True)
    await query.answer()


@dp.callback_query(F.data == "edit:contact", OrderForm.confirm)
async def edit_contact(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=True)
    await move_to_contact(query.message, state, edit_existing=True)
    await query.answer()


@dp.callback_query(F.data == "order:cancel", F.message.chat.type == ChatType.PRIVATE)
async def cancel_order_callback(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(STORAGE.delete_order_draft, query.from_user.id)
    await show_home(user_id=query.from_user.id, chat_id=query.message.chat.id,
                    bot=query.bot, target_message=query.message)
    await query.answer("Черновик отменён")


@dp.callback_query(F.data == "order:confirm", OrderForm.confirm, F.message.chat.type == ChatType.PRIVATE)
async def confirm_order(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    required = ("service_id", "service_title", "description", "contact")
    if not all(data.get(key) for key in required):
        await query.answer("Не хватает данных. Начни заполнение заново.", show_alert=True)
        await state.clear()
        await query.message.edit_text("Не удалось восстановить все данные. Нажми /start и начни заново.")
        return
    user = query.from_user
    submission_key = str(data.get("submission_key") or uuid4().hex)
    order_id = await asyncio.to_thread(
        STORAGE.create_order,
        user_id=user.id,
        username=user.username,
        customer_name=user.full_name,
        service_id=data["service_id"],
        service_title=data["service_title"],
        description=data["description"],
        features=data.get("features", "Не указаны"),
        budget=data.get("budget", "Не указан"),
        deadline=data.get("deadline", "Не указан"),
        repository_url=data.get("repository_url", ""),
        contact=data["contact"],
        submission_key=submission_key,
    )
    await state.clear()
    await asyncio.to_thread(STORAGE.delete_order_draft, user.id)
    latest_order = await asyncio.to_thread(STORAGE.get_user_order, order_id, user.id)
    await query.message.edit_text(
        f"✅ <b>Заявка #{order_id} принята</b>\n\n" + home_text(latest_order),
        reply_markup=main_menu(order_id),
    )
    await remember_panel(user.id, query.message.chat.id, query.message.message_id)
    await query.answer("Заявка сохранена")
    await notify_admins(bot, order_id)


@dp.callback_query(F.data == "menu:services", F.message.chat.type == ChatType.PRIVATE)
async def show_services(query: CallbackQuery) -> None:
    await query.message.edit_text(
        "<b>Услуги BOTVEX</b>\n\n"
        "Выбери направление, чтобы посмотреть состав работы и способ оценки. "
        "Окончательная стоимость согласуется после уточнения требований.",
        reply_markup=service_catalog_menu(SERVICES),
    )
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("serviceinfo:"), F.message.chat.type == ChatType.PRIVATE)
async def show_service_detail(query: CallbackQuery) -> None:
    service_id = query.data.split(":", 1)[1]
    service = service_by_id(SERVICES, service_id)
    if not service:
        await query.answer("Услуга не найдена. Обнови каталог.", show_alert=True)
        return
    text = (
        f"<b>{safe(service['title'])}</b>\n\n"
        f"{safe(service['description'])}\n\n"
        f"<b>Стоимость:</b> {safe(service['price'])}\n\n"
        "Перед началом уточним требования, согласуем объём работ и стоимость. "
        "Срок зависит от задачи и внешних интеграций."
    )
    await query.message.edit_text(text, reply_markup=service_detail_menu(service_id))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("serviceorder:"), F.message.chat.type == ChatType.PRIVATE)
async def order_selected_service(query: CallbackQuery, state: FSMContext) -> None:
    service_id = query.data.split(":", 1)[1]
    service = service_by_id(SERVICES, service_id)
    if not service:
        await query.answer("Услуга не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(editing=False, submission_key=uuid4().hex,
                            service_id=service["id"], service_title=service["title"])
    await state.set_state(OrderForm.description)
    await query.message.edit_text(
        f"Выбрано: <b>{safe(service['title'])}</b>\n\n"
        "<b>Шаг 2/7 · Опиши задачу</b>\n"
        "Что должно делать решение и какую проблему оно должно решить? (5–1500 символов)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К услуге", callback_data=f"serviceinfo:{service_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]),
    )
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await persist_order_draft(query.from_user.id, state, chat_id=query.message.chat.id)
    await query.answer()


@dp.callback_query(F.data == "menu:portfolio", F.message.chat.type == ChatType.PRIVATE)
async def show_portfolio(query: CallbackQuery) -> None:
    text = (
        "<b>Примеры работ BOTVEX</b>\n\n"
        "Я развиваю портфолио Python-разработчика и публикую открытый код, чтобы можно было "
        "посмотреть структуру проектов и подход к реализации. Учебные и демонстрационные проекты "
        "не выдаются за завершённые коммерческие кейсы.\n\n"
        "Перед началом заказа уточняем задачу, согласуем объём, стоимость и критерии результата. "
        "По ходу работы можно обсудить технические решения и получить понятное объяснение реализации."
    )
    rows = []
    if GITHUB_URL:
        rows.append([InlineKeyboardButton(text="↗ Открытый код на GitHub", url=GITHUB_URL, style="primary")])
    else:
        text += "\n\n<i>Ссылка на GitHub пока не настроена.</i>"
    rows.extend([
        [InlineKeyboardButton(text="🚀 Обсудить похожий проект", callback_data="order:new", style="primary")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data == "menu:contact", F.message.chat.type == ChatType.PRIVATE)
async def show_contact(query: CallbackQuery) -> None:
    latest = await asyncio.to_thread(STORAGE.get_latest_user_order, query.from_user.id)
    target = f"https://t.me/{DEVELOPER_USERNAME}" if DEVELOPER_USERNAME else "https://t.me/"
    rows = [
        [InlineKeyboardButton(text="💬 Написать разработчику", url=target, style="primary")],
    ]
    if latest:
        rows.append([InlineKeyboardButton(
            text=f"📦 Открыть последний заказ #{latest['id']}",
            callback_data=f"my:view:{latest['id']}", style="primary",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    await query.message.edit_text("<b>Связаться с разработчиком</b>\n\nНапиши, если хочешь уточнить детали до оформления заявки.", reply_markup=keyboard)
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data == "menu:home", F.message.chat.type == ChatType.PRIVATE)
async def menu_home(query: CallbackQuery, state: FSMContext) -> None:
    await clear_customer_notifications(
        query.bot, query.from_user.id, exclude_message_id=query.message.message_id
    )
    await state.clear()
    await show_home(user_id=query.from_user.id, chat_id=query.message.chat.id,
                    bot=query.bot, target_message=query.message)
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data == "my:orders", F.message.chat.type == ChatType.PRIVATE)
async def my_orders(query: CallbackQuery) -> None:
    await clear_customer_notifications(
        query.bot, query.from_user.id, exclude_message_id=query.message.message_id
    )
    orders = await asyncio.to_thread(STORAGE.list_user_orders, query.from_user.id, 10)
    if not orders:
        await query.message.edit_text("У тебя пока нет заявок. Нажми «Заказать разработку», чтобы создать первую.", reply_markup=main_menu())
    else:
        await query.message.edit_text("<b>Твои заявки</b>\nВыбери заявку, чтобы посмотреть детали и статус:", reply_markup=my_orders_menu(orders))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("my:view:"), F.message.chat.type == ChatType.PRIVATE)
async def view_my_order(query: CallbackQuery) -> None:
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    if not order:
        await query.answer("Заявка не найдена или недоступна", show_alert=True)
        return
    await clear_customer_notifications(query.bot, query.from_user.id, order_id=order_id,
                                       exclude_message_id=query.message.message_id)
    cancellable = order["status"] in {"new", "under_review", "needs_info", "quote_sent", "awaiting_client"}
    active_clarification = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=True)
    await query.message.edit_text(
        customer_order_text(order, events),
        reply_markup=my_order_actions(
            order_id, cancellable,
            has_open_clarification=bool(active_clarification and order["status"] == "needs_info"),
        ),
    )
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("mycancel:ask:"), F.message.chat.type == ChatType.PRIVATE)
async def ask_cancel_my_order(query: CallbackQuery) -> None:
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    if not order or order["status"] not in {"new", "under_review", "needs_info", "quote_sent", "awaiting_client"}:
        await query.answer("Эту заявку уже нельзя отменить через меню", show_alert=True)
        return
    await query.message.edit_text(f"Точно отменить заявку <b>#{order_id}</b>?", reply_markup=customer_cancel_confirm(order_id))
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("mycancel:confirm:"), F.message.chat.type == ChatType.PRIVATE)
async def confirm_cancel_my_order(query: CallbackQuery, bot: Bot) -> None:
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    if not order or order["status"] not in {"new", "under_review", "needs_info", "quote_sent", "awaiting_client"}:
        await query.answer("Эту заявку уже нельзя отменить", show_alert=True)
        return
    try:
        result = await asyncio.to_thread(
            STORAGE.transition_status, order_id, "cancelled", actor_type="customer",
            actor_id=query.from_user.id, message="Клиент отменил заявку.", customer_visible=True,
        )
    except ValueError:
        await query.answer("Нельзя отменить заказ на текущем этапе", show_alert=True)
        return
    if not result:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    latest_order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    await query.message.edit_text(
        f"Заявка <b>#{order_id}</b> отменена.\n\n" + home_text(latest_order),
        reply_markup=main_menu(order_id),
    )
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    for admin_id in sorted(ADMIN_IDS):
        try:
            await bot.send_message(
                admin_id, f"Клиент отменил заявку <b>#{order_id}</b>.",
                reply_markup=admin_notification_menu(order_id),
            )
        except TelegramAPIError:
            log.warning("Failed to notify admin ID %s about customer cancellation for order #%s", admin_id, order_id)
    await query.answer("Заявка отменена")


@dp.callback_query(F.data.startswith("customer:open:"), F.message.chat.type == ChatType.PRIVATE)
async def open_customer_notification(query: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, raw_order_id, raw_notification_id = query.data.split(":", 3)
        order_id = int(raw_order_id)
        notification_id = int(raw_notification_id)
    except (ValueError, TypeError):
        await query.answer("Ссылка на уведомление некорректна", show_alert=True)
        return
    notification = await asyncio.to_thread(STORAGE.get_customer_notification, notification_id)
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    if not notification or not order or int(notification["user_id"]) != query.from_user.id or int(notification["order_id"]) != order_id:
        await query.answer("Уведомление или заявка недоступны", show_alert=True)
        return
    # A notification can interrupt a draft or another text-driven workflow. Keep the
    # persisted draft in SQLite, but clear in-memory FSM state so future text isn't
    # accidentally consumed by the previous form.
    await state.clear()
    await clear_customer_notifications(query.bot, query.from_user.id, order_id=order_id,
                                       exclude_message_id=query.message.message_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=True, limit=250)
    active_clarification = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    markup = my_order_actions(
        order_id, order["status"] not in {"completed", "cancelled"},
        has_open_clarification=bool(active_clarification and order["status"] == "needs_info"),
    )
    try:
        await query.message.edit_text(customer_order_text(order, events), reply_markup=markup)
        await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
        await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    except TelegramAPIError as exc:
        log.debug("Could not transform notification into order panel (%s)", type(exc).__name__)
        try:
            await query.message.delete()
        except TelegramAPIError:
            pass
        sent = await query.bot.send_message(query.message.chat.id, customer_order_text(order, events), reply_markup=markup)
        await remember_panel(query.from_user.id, query.message.chat.id, sent.message_id)
        await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    await query.answer("Открыта заявка")


async def _show_admin_clarification_form(query: CallbackQuery, state: FSMContext, order_id: int) -> bool:
    """Open an explicit clarification composer, or show an existing active question."""
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return True
    if order["status"] in {"completed", "cancelled"}:
        await query.answer("Для завершённой или отменённой заявки уточнение недоступно", show_alert=True)
        return True
    active = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    if active:
        await query.message.edit_text(
            f"<b>Уточнение по заявке #{order_id}</b>\n\n"
            f"{safe_limit(active['question_text'], 1400)}\n\n"
            "Ожидается ответ клиента. Можно отправить напоминание или вернуться к карточке.",
            reply_markup=admin_clarification_view_menu(order_id, True, can_create=False),
        )
        await query.answer("У заявки уже есть активное уточнение")
        return True
    await state.clear()
    await state.set_state(AdminClarification.question)
    await state.update_data(
        admin_order_id=order_id,
        admin_panel_chat_id=query.message.chat.id,
        admin_panel_message_id=query.message.message_id,
    )
    await query.message.edit_text(
        f"<b>Запросить уточнение · заявка #{order_id}</b>\n\n"
        "Напиши один конкретный вопрос клиенту. Он получит его в уведомлении и сможет ответить прямо в боте.\n\n"
        "От 3 до 1500 символов. Вводимое сообщение удалится после обработки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:clarify:cancel:{order_id}", style="danger")]
        ]),
    )
    await query.answer()
    return False


@dp.callback_query(F.data.startswith("admin:clarify:start:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_clarification_start(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    await _show_admin_clarification_form(query, state, order_id)


@dp.callback_query(F.data.startswith("admin:clarify:view:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_clarification_view(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    active = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    if active:
        text = (
            f"<b>Уточнение по заявке #{order_id}</b>\n\n"
            f"<b>Вопрос клиенту:</b>\n{safe_limit(active['question_text'], 1400)}\n\n"
            f"Создано: {safe(format_time(active['created_at_utc']))}\n"
            "Состояние: <b>Ожидается ответ клиента</b>"
        )
    else:
        text = f"<b>Уточнение по заявке #{order_id}</b>\n\nАктивного запроса нет."
    await query.message.edit_text(
        text,
        reply_markup=admin_clarification_view_menu(
            order_id, bool(active), can_create=order["status"] not in {"completed", "cancelled"}
        ),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("admin:clarify:cancel:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_clarification_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    data = await state.get_data()
    if data.get("admin_order_id") and int(data["admin_order_id"]) != order_id:
        await query.answer("Эта форма относится к другой заявке", show_alert=True)
        return
    await state.clear()
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    await query.message.edit_text(admin_order_text(order, events), reply_markup=admin_order_menu(order_id, order["status"]))
    await query.answer("Запрос не отправлен")


@dp.message(AdminClarification.question, F.text, F.chat.type == ChatType.PRIVATE)
async def receive_admin_clarification_question(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    question = message.text.strip()
    data = await state.get_data()
    order_id = int(data.get("admin_order_id", 0))
    if len(question) < 3 or len(question) > 1500:
        await edit_panel_by_state(
            bot, data,
            f"<b>Запросить уточнение · заявка #{order_id}</b>\n\n"
            "Вопрос должен содержать от 3 до 1500 символов. Напиши его ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:clarify:cancel:{order_id}", style="danger")
            ]]),
        )
        return
    if not await asyncio.to_thread(STORAGE.get_order, order_id):
        await state.clear()
        return
    await state.update_data(clarification_question=question)
    await state.set_state(AdminClarification.confirm)
    await edit_panel_by_state(
        bot, await state.get_data(),
        f"<b>Проверь уточнение · заявка #{order_id}</b>\n\n"
        f"<b>Вопрос клиенту:</b>\n{safe_limit(question, 1400)}\n\n"
        "После подтверждения вопрос сохранится в истории и клиент получит уведомление.",
        admin_clarification_confirm_menu(order_id),
    )


@dp.callback_query(F.data.startswith("admin:clarify:confirm:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_clarification_confirm(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    data = await state.get_data()
    if await state.get_state() != AdminClarification.confirm.state or int(data.get("admin_order_id", 0)) != order_id:
        await query.answer("Форма устарела. Открой карточку заявки заново.", show_alert=True)
        return
    question = str(data.get("clarification_question", "")).strip()
    try:
        clarification = await asyncio.to_thread(
            STORAGE.create_clarification, order_id, query.from_user.id, question
        )
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        await state.clear()
        return
    if not clarification:
        await query.answer("Заявка не найдена", show_alert=True)
        await state.clear()
        return
    await state.clear()
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    await query.message.edit_text(admin_order_text(order, events), reply_markup=admin_order_menu(order_id, order["status"]))
    delivered = await queue_customer_notification(
        bot, order_id=order_id, user_id=int(clarification["user_id"]),
        notification_type="clarification_requested",
        event_key=f"clarification:{clarification['id']}:requested",
        body=(
            f"<b>BOTVEX · Уточнение по заявке #{order_id}</b>\n\n"
            f"<b>Вопрос разработчика:</b>\n{safe_limit(question, 1400)}\n\n"
            "Открой заявку, чтобы посмотреть детали и отправить ответ."
        ),
    )
    await query.answer(
        "Уточнение отправлено клиенту" if delivered else "Уточнение сохранено, но уведомление не доставлено. Повтори доставку из панели.",
        show_alert=not delivered,
    )


@dp.callback_query(F.data.startswith("admin:clarify:remind:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_clarification_remind(query: CallbackQuery, bot: Bot) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    active = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    if not order or not active:
        await query.answer("Активного уточнения нет", show_alert=True)
        return
    result = await queue_customer_notification(
        bot, order_id=order_id, user_id=int(order["user_id"]),
        notification_type="clarification_requested",
        event_key=f"clarification:{active['id']}:remind:{uuid4().hex}",
        body=(
            f"<b>BOTVEX · Напоминание по заявке #{order_id}</b>\n\n"
            f"{safe_limit(active['question_text'], 1400)}\n\n"
            "Если вопрос ещё актуален, открой заявку и отправь ответ."
        ),
    )
    await query.answer("Напоминание отправлено" if result else "Не удалось доставить уведомление", show_alert=not result)


@dp.callback_query(F.data.startswith("customer:clarify:answer:"), F.message.chat.type == ChatType.PRIVATE)
async def customer_clarification_start(query: CallbackQuery, state: FSMContext) -> None:
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    active = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    if not order or not active or order["status"] != "needs_info":
        await query.answer("По этой заявке сейчас не ожидается уточнение", show_alert=True)
        return
    await clear_customer_notifications(
        query.bot, query.from_user.id, order_id=order_id,
        exclude_message_id=query.message.message_id,
    )
    await state.clear()
    await state.set_state(CustomerClarification.answer)
    await state.update_data(
        clarification_order_id=order_id, clarification_id=int(active["id"]),
        admin_panel_chat_id=query.message.chat.id, admin_panel_message_id=query.message.message_id,
    )
    await query.message.edit_text(
        f"<b>Ответ на уточнение · заявка #{order_id}</b>\n\n"
        f"<b>Вопрос разработчика:</b>\n{safe_limit(active['question_text'], 1400)}\n\n"
        "Отправь ответ одним сообщением (до 3000 символов). После сохранения сообщение будет удалено из чата по возможности.",
        reply_markup=customer_clarification_cancel_menu(order_id),
    )
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("customer:clarify:cancel:"), F.message.chat.type == ChatType.PRIVATE)
async def customer_clarification_cancel(query: CallbackQuery, state: FSMContext) -> None:
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, query.from_user.id)
    if not order:
        await query.answer("Заявка недоступна", show_alert=True)
        return
    data = await state.get_data()
    if data.get("clarification_order_id") and int(data["clarification_order_id"]) != order_id:
        await query.answer("Форма относится к другой заявке", show_alert=True)
        return
    await state.clear()
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=True)
    active = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    markup = my_order_actions(
        order_id, order["status"] not in {"completed", "cancelled"},
        has_open_clarification=bool(active and order["status"] == "needs_info"),
    )
    await query.message.edit_text(customer_order_text(order, events), reply_markup=markup)
    await remember_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await mark_notification_message_processed(query.from_user.id, query.message.message_id)
    await query.answer("Ответ отменён")


@dp.message(CustomerClarification.answer, F.text, F.chat.type == ChatType.PRIVATE)
async def receive_customer_clarification_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    order_id = int(data.get("clarification_order_id", 0))
    clarification_id = int(data.get("clarification_id", 0))
    answer_text = message.text.strip()
    if not answer_text or len(answer_text) > 3000:
        await edit_panel_by_state(
            bot, data,
            f"<b>Ответ на уточнение · заявка #{order_id}</b>\n\n"
            "Ответ должен содержать от 1 до 3000 символов. Отправь его ещё раз.",
            customer_clarification_cancel_menu(order_id),
        )
        return
    order = await asyncio.to_thread(STORAGE.get_user_order, order_id, message.from_user.id)
    if not order:
        await state.clear()
        return
    try:
        result = await asyncio.to_thread(
            STORAGE.answer_clarification, clarification_id, order_id, message.from_user.id, answer_text
        )
    except ValueError:
        await state.clear()
        await edit_panel_by_state(bot, data, "Это уточнение уже закрыто. Открой заявку, чтобы увидеть актуальное состояние.", None)
        return
    if not result:
        await state.clear()
        await edit_panel_by_state(bot, data, "Заявка или уточнение больше недоступны.", None)
        return
    await state.clear()
    updated = await asyncio.to_thread(STORAGE.get_user_order, order_id, message.from_user.id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=True, limit=250)
    active = await asyncio.to_thread(STORAGE.get_active_clarification, order_id)
    text = "<b>Ответ сохранён.</b> Разработчик получит его и продолжит работу.\n\n" + customer_order_text(updated, events)
    markup = my_order_actions(
        order_id, updated["status"] not in {"completed", "cancelled"},
        has_open_clarification=bool(active and updated["status"] == "needs_info"),
    )
    if not await edit_panel_by_state(bot, data, text, markup):
        sent = await bot.send_message(message.chat.id, text, reply_markup=markup)
        await remember_panel(message.from_user.id, message.chat.id, sent.message_id)
    else:
        await remember_panel(message.from_user.id, int(data.get("admin_panel_chat_id") or message.chat.id), int(data.get("admin_panel_message_id") or 0))
    for admin_id in sorted(ADMIN_IDS):
        try:
            await bot.send_message(
                admin_id,
                f"<b>Ответ на уточнение · заявка #{order_id}</b>\n\n"
                f"{safe_limit(answer_text, 1800)}",
                reply_markup=admin_notification_menu(order_id),
            )
        except TelegramAPIError as exc:
            log.warning("Failed to notify admin about clarification answer for order #%s (%s)", order_id, type(exc).__name__)


@dp.callback_query(F.data.startswith("admin:view:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_view_order(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    current_state = await state.get_state()
    if current_state and current_state.startswith("AdminPrice:"):
        await state.clear()
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await adopt_admin_notification_panel(query, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    await query.message.edit_text(
        admin_order_text(order, events),
        reply_markup=admin_order_menu(order_id, order["status"]),
    )
    await remember_admin_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.callback_query(F.data.startswith("admin:status:"), F.message.chat.type == ChatType.PRIVATE)
async def choose_order_status(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await query.message.edit_text(
        f"<b>Изменение статуса · заявка #{order_id}</b>\n\n"
        f"Текущий этап: <b>{safe(STATUS_LABELS.get(order['status'], order['status']))}</b>\n"
        "Выбери допустимый следующий этап:",
        reply_markup=admin_status_menu(order_id, order["status"]),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("status:"), F.message.chat.type == ChatType.PRIVATE)
async def change_order_status(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа: действие доступно только администратору.", show_alert=True)
        return
    try:
        _, raw_id, status = query.data.split(":", 2)
        order_id = int(raw_id)
        if status not in STATUS_LABELS:
            raise ValueError("unknown status")
    except (ValueError, TypeError):
        await query.answer("Некорректная команда", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    if status == "needs_info":
        await _show_admin_clarification_form(query, state, order_id)
        return
    try:
        result = await asyncio.to_thread(
            STORAGE.transition_status, order_id, status, actor_type="admin",
            actor_id=query.from_user.id,
            message=f"Статус изменён на «{STATUS_LABELS[status]}».", customer_visible=True,
        )
    except ValueError:
        await query.answer(
            f"Переход из «{STATUS_LABELS.get(order['status'], order['status'])}» в "
            f"«{STATUS_LABELS[status]}» не разрешён.", show_alert=True,
        )
        return
    if not result:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    if not result["changed"]:
        await query.answer("Этот статус уже установлен")
        return
    await asyncio.to_thread(STORAGE.cancel_open_clarification, order_id)
    updated = await asyncio.to_thread(STORAGE.get_order, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    await query.message.edit_text(
        admin_order_text(updated, events),
        reply_markup=admin_order_menu(order_id, status),
    )
    await queue_customer_notification(
        bot, order_id=order_id, user_id=int(updated["user_id"]),
        notification_type="status_changed", event_key=f"status:{order_id}:{result['created_at']}",
        body=(
            f"<b>BOTVEX · Обновление заявки #{order_id}</b>\n\n"
            f"Новый этап: <b>{safe(STATUS_LABELS[status])}</b>\n"
            f"Обновлено: {safe(format_time(updated['updated_at']))}\n\n"
            "Открой карточку, чтобы увидеть актуальные данные и историю."
        ),
    )
    await query.answer(f"Статус: {STATUS_LABELS[status]}")


@dp.callback_query(F.data.startswith("admin:price:"), F.message.chat.type == ChatType.PRIVATE)
async def start_price_assignment(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = query.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order_id = int(parts[2])
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminPrice.amount)
    await state.update_data(
        admin_order_id=order_id, admin_panel_chat_id=query.message.chat.id,
        admin_panel_message_id=query.message.message_id, price_note="",
    )
    current_price = format_price(order["price_amount"], order.get("price_currency") or CURRENCY) if order.get("price_amount") else "ещё не назначена"
    await query.message.edit_text(
        f"<b>Назначение цены · заявка #{order_id}</b>\n\n"
        f"Текущая цена: {safe(current_price)}\n"
        f"Введи положительную сумму в {safe(CURRENCY)}. Пример: <code>15000</code> или <code>15000,50</code>.\n"
        "Текстовый ввод удалится после успешной проверки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:view:{order_id}", style="danger")]
        ]),
    )
    await query.answer()


@dp.message(AdminPrice.amount, F.text, F.chat.type == ChatType.PRIVATE)
async def receive_price_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data = await state.get_data()
    order_id = int(data.get("admin_order_id", 0))
    raw = message.text.strip().replace(" ", "").replace("₽", "").replace("RUB", "").replace("rub", "")
    try:
        amount = Decimal(raw.replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000") or amount.as_tuple().exponent < -2:
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await edit_panel_by_state(
            bot, data,
            f"<b>Назначение цены · заявка #{order_id}</b>\n\n" 
            "Сумма должна быть положительной, не превышать 1 000 000 000 и содержать не более двух знаков после запятой. Попробуй ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:view:{order_id}", style="danger")]]),
        )
        return
    await state.update_data(price_amount=format(amount, "f"))
    await state.set_state(AdminPrice.note)
    updated_data = await state.get_data()
    await edit_panel_by_state(
        bot, updated_data,
        f"<b>Цена · заявка #{order_id}</b>\n\n"
        f"Сумма: <b>{safe(format_price(format(amount, 'f'), CURRENCY))}</b>\n\n"
        "Добавь краткое пояснение, что входит в стоимость, или пропусти этот шаг.",
        price_note_menu(order_id),
    )


@dp.message(AdminPrice.note, F.text, F.chat.type == ChatType.PRIVATE)
async def receive_price_note(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    note = message.text.strip()
    if len(note) > 1000:
        data = await state.get_data()
        order_id = int(data.get("admin_order_id", 0))
        await edit_panel_by_state(
            bot, data,
            f"<b>Цена · заявка #{order_id}</b>\n\nПояснение должно быть не длиннее 1000 символов. Отправь его ещё раз или пропусти шаг.",
            price_note_menu(order_id),
        )
        return
    data = await state.get_data()
    order_id = int(data.get("admin_order_id", 0))
    await state.update_data(price_note=note)
    await state.set_state(AdminPrice.confirm)
    new_data = await state.get_data()
    await edit_panel_by_state(
        bot, new_data,
        f"<b>Проверь цену · заявка #{order_id}</b>\n\n"
        f"Стоимость: <b>{safe(format_price(new_data['price_amount'], CURRENCY))}</b>\n"
        f"Пояснение: {safe_limit(note or 'не указано', 800)}\n\n"
        "После подтверждения цена сохранится в заявке, попадёт в историю и будет отправлена клиенту.",
        price_confirm_menu(order_id),
    )


@dp.callback_query(F.data.startswith("admin:price-note-skip:"), AdminPrice.note, F.message.chat.type == ChatType.PRIVATE)
async def skip_price_note(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    data = await state.get_data()
    if int(data.get("admin_order_id", 0)) != order_id or not data.get("price_amount"):
        await query.answer("Действие устарело. Открой заявку заново.", show_alert=True)
        return
    await state.update_data(price_note="")
    await state.set_state(AdminPrice.confirm)
    data = await state.get_data()
    await query.message.edit_text(
        f"<b>Проверь цену · заявка #{order_id}</b>\n\n"
        f"Стоимость: <b>{safe(format_price(data['price_amount'], CURRENCY))}</b>\n"
        "Пояснение: не указано\n\nПосле подтверждения цена сохранится и будет отправлена клиенту.",
        reply_markup=price_confirm_menu(order_id),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("admin:price-confirm:"), AdminPrice.confirm, F.message.chat.type == ChatType.PRIVATE)
async def confirm_price_assignment(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    data = await state.get_data()
    if int(data.get("admin_order_id", 0)) != order_id or not data.get("price_amount"):
        await query.answer("Данные цены устарели. Начни назначение заново.", show_alert=True)
        return
    result = await asyncio.to_thread(
        STORAGE.set_order_price, order_id, data["price_amount"], CURRENCY,
        data.get("price_note", ""), admin_id=query.from_user.id,
    )
    if not result:
        await state.clear()
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await state.clear()
    if result.get("changed"):
        note = result.get("note")
        body = (
            f"<b>BOTVEX · Стоимость по заявке #{order_id}</b>\n\n"
            f"Предложенная стоимость: <b>{safe(format_price(result['new_price'], result['currency']))}</b>\n"
            + (f"Пояснение: {safe_limit(note, 1000)}\n\n" if note else "\n")
            + "Открой заявку, чтобы посмотреть цену и историю изменений."
        )
        await queue_customer_notification(
            bot, order_id=order_id, user_id=int(result["user_id"]),
            notification_type="price_assigned", event_key=f"price:{order_id}:{result['created_at']}", body=body,
        )
    updated = await asyncio.to_thread(STORAGE.get_order, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    await query.message.edit_text(admin_order_text(updated, events), reply_markup=admin_order_menu(order_id, updated["status"]))
    await query.answer("Цена сохранена и уведомление поставлено на доставку" if result.get("changed") else "Цена не изменилась")


@dp.callback_query(F.data.startswith("admin:history:"), F.message.chat.type == ChatType.PRIVATE)
async def admin_order_history(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    await query.message.edit_text(
        f"<b>История заявки #{order_id}</b>\nЭтап: <b>{safe(STATUS_LABELS.get(order['status'], order['status']))}</b>\n\n"
        + timeline_text(events, customer_view=False, max_events=30),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 К заявке", callback_data=f"admin:view:{order_id}")],
            [InlineKeyboardButton(text="🛠 Панель", callback_data="admin:dashboard")],
        ]),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("admin:dismiss:"), F.message.chat.type == ChatType.PRIVATE)
async def dismiss_admin_notification(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    delivery = await asyncio.to_thread(STORAGE.get_notification_delivery, order_id, query.from_user.id)
    if delivery and int(delivery.get("message_id") or 0) == query.message.message_id:
        await asyncio.to_thread(STORAGE.dismiss_notification, order_id, query.from_user.id)
    try:
        await query.bot.delete_message(query.message.chat.id, query.message.message_id)
    except TelegramAPIError as exc:
        log.info("Could not delete admin notification #%s (%s); trying to remove its buttons", order_id, type(exc).__name__)
        try:
            await query.message.edit_text(
                f"Уведомление по заявке <b>#{order_id}</b> убрано.\n"
                "Сама заявка и история сохранены. Найти её можно через /admin.",
                reply_markup=None,
            )
        except TelegramAPIError:
            log.warning("Could neither delete nor edit notification for order #%s", order_id)
    await query.answer("Уведомление убрано; заявка сохранена")


@dp.callback_query(F.data.startswith("admin:note:"), F.message.chat.type == ChatType.PRIVATE)
async def start_admin_note(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    if not await asyncio.to_thread(STORAGE.get_order, order_id):
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await state.set_state(AdminNote.text)
    await state.update_data(
        admin_order_id=order_id, admin_panel_chat_id=query.message.chat.id,
        admin_panel_message_id=query.message.message_id,
    )
    await query.message.edit_text(
        f"<b>Внутренняя заметка · заявка #{order_id}</b>\n\n"
        "Отправь заметку длиной до 2000 символов. Текст будет сохранён только для администратора.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:view:{order_id}", style="danger")]]),
    )
    await remember_admin_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer()


@dp.message(AdminNote.text, F.text, F.chat.type == ChatType.PRIVATE)
async def save_admin_note(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data = await state.get_data()
    order_id = int(data.get("admin_order_id", 0))
    note = message.text.strip()
    if len(note) < 1 or len(note) > 2000:
        await edit_panel_by_state(
            bot, data,
            f"<b>Внутренняя заметка · заявка #{order_id}</b>\n\nЗаметка должна содержать 1–2000 символов. Отправь её ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:view:{order_id}", style="danger")]]),
        )
        return
    if not await asyncio.to_thread(STORAGE.add_admin_note, order_id, message.from_user.id, note):
        await state.clear()
        await edit_panel_by_state(bot, data, "Заявка не найдена. Вернись в панель и выбери заявку заново.", admin_dashboard_menu())
        return
    await state.clear()
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    if order:
        await edit_panel_by_state(bot, data, admin_order_text(order, events), admin_order_menu(order_id, order["status"]))
    await remember_admin_panel(message.from_user.id, int(data.get("admin_panel_chat_id") or message.chat.id), int(data.get("admin_panel_message_id") or 0))


@dp.callback_query(F.data.startswith("admin:reply:"), F.message.chat.type == ChatType.PRIVATE)
async def start_admin_reply(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        order_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер заявки", show_alert=True)
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminReply.text)
    await state.update_data(
        admin_order_id=order_id, target_user_id=int(order["user_id"]),
        admin_panel_chat_id=query.message.chat.id, admin_panel_message_id=query.message.message_id,
    )
    await query.message.edit_text(
        f"<b>Ответ клиенту · заявка #{order_id}</b>\n\n"
        "Напиши сообщение клиенту. Перед отправкой оно сохранится в истории заявки, а клиентское меню будет заменено уведомлением.\n\n"
        "От 1 до 3000 символов.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:view:{order_id}", style="danger")
        ]]),
    )
    await query.answer()


@dp.message(AdminReply.text, F.text, F.chat.type == ChatType.PRIVATE)
async def send_admin_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data = await state.get_data()
    order_id = int(data.get("admin_order_id", 0))
    user_id = int(data.get("target_user_id", 0))
    reply_text = message.text.strip()
    if not reply_text or len(reply_text) > 3000:
        await edit_panel_by_state(
            bot, data,
            f"<b>Ответ клиенту · заявка #{order_id}</b>\n\nОтвет должен содержать от 1 до 3000 символов. Отправь его ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:view:{order_id}", style="danger")
            ]]),
        )
        return
    order = await asyncio.to_thread(STORAGE.get_order, order_id)
    if not order or int(order["user_id"]) != user_id:
        await state.clear()
        await edit_panel_by_state(bot, data, "Не удалось подтвердить получателя. Открой карточку заявки заново.", None)
        return
    # Store the customer-visible event before attempting network delivery so the reply remains auditable.
    await asyncio.to_thread(
        STORAGE.add_event, order_id, actor_type="admin", actor_id=message.from_user.id,
        event_type="admin_reply", message=f"Сообщение разработчика: {reply_text}", customer_visible=True,
    )
    await asyncio.to_thread(
        STORAGE.add_admin_note, order_id, message.from_user.id,
        f"Подготовлен ответ клиенту: {reply_text[:1500]}",
    )
    delivered = await queue_customer_notification(
        bot, order_id=order_id, user_id=user_id,
        notification_type="admin_reply",
        event_key=f"admin-reply:{order_id}:{message.message_id}",
        body=(
            f"<b>BOTVEX · Сообщение по заявке #{order_id}</b>\n\n"
            f"{safe_limit(reply_text, 2500)}\n\n"
            "Нажми «Открыть заявку», чтобы увидеть актуальную карточку и историю."
        ),
    )
    await state.clear()
    refreshed = await asyncio.to_thread(STORAGE.get_order, order_id)
    events = await asyncio.to_thread(STORAGE.list_order_events, order_id, customer_visible_only=False, limit=250)
    result_text = admin_order_text(refreshed, events)
    result_text += "\n\n" + ("✅ Уведомление доставлено клиенту." if delivered else "⚠️ Ответ сохранён, доставка уведомления ожидает повтора.")
    await edit_panel_by_state(bot, data, result_text, admin_order_menu(order_id, refreshed["status"]))


async def configure_bot_commands(bot: Bot) -> None:
    """Show admin commands only in configured admin chats; authorization still lives in handlers."""
    user_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="cancel", description="Отменить текущий сценарий"),
    ]
    admin_commands = user_commands + [
        BotCommand(command="admin", description="Панель администратора"),
        BotCommand(command="orders", description="Все заявки"),
        BotCommand(command="stats", description="Статистика заявок"),
        BotCommand(command="export", description="Экспорт заявок в CSV"),
        BotCommand(command="retry_notifications", description="Повторить уведомления"),
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        for admin_id in sorted(ADMIN_IDS):
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
    except TelegramAPIError as exc:
        # Command-menu configuration is optional; it must not stop the order bot.
        log.warning("Could not configure Telegram command menu (%s)", type(exc).__name__)


async def retry_failed_notifications(bot: Bot, limit: int = 50) -> tuple[int, int]:
    pending = await asyncio.to_thread(STORAGE.list_pending_notifications, limit)
    attempted = 0
    sent = 0
    for order in pending:
        attempted += 1
        if await notify_admins(bot, int(order["id"])):
            sent += 1
    customer_pending = await asyncio.to_thread(STORAGE.list_pending_customer_notifications, limit)
    for notification in customer_pending:
        attempted += 1
        if await deliver_customer_notification(bot, int(notification["id"])):
            sent += 1
    return sent, attempted


@dp.callback_query(F.data == "admin:retry-notifications", F.message.chat.type == ChatType.PRIVATE)
async def admin_retry_notifications_callback(query: CallbackQuery) -> None:
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Нет доступа", show_alert=True)
        return
    sent, attempted = await retry_failed_notifications(query.bot)
    stats = await asyncio.to_thread(STORAGE.get_stats)
    result_text = admin_dashboard_text(stats) + f"\n\nПовтор доставки: успешно {sent} из {attempted}."
    try:
        await query.message.edit_text(result_text, reply_markup=admin_dashboard_menu())
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise
    await remember_admin_panel(query.from_user.id, query.message.chat.id, query.message.message_id)
    await query.answer("Проверка уведомлений завершена")


@dp.message(F.chat.type == ChatType.PRIVATE)
async def ignore_unhandled_private_message(message: Message) -> None:
    """Silently absorb unknown private messages; the cleanup middleware deletes them best-effort."""
    return None


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Скопируй .env.example в .env и заполни BOT_TOKEN локально.")
    await asyncio.to_thread(STORAGE.initialize)
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пустой: клиентский сценарий запустится, но уведомления администраторам работать не будут.")
    else:
        log.info("Loaded %s administrator ID(s)", len(ADMIN_IDS))
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await configure_bot_commands(bot)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
