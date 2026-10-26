"""Reusable, compact Telegram keyboards for BOTVEX.

Telegram's standard inline button styles are semantic presets, not arbitrary RGB
colors. Button labels remain meaningful even in clients that render styles subtly.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.storage import ALLOWED_TRANSITIONS

STATUS_LABELS = {
    "new": "Новая",
    "under_review": "На рассмотрении",
    "needs_info": "Нужно уточнение",
    "quote_sent": "Оценка подготовлена",
    "awaiting_client": "Ожидает клиента",
    "in_progress": "В работе",
    "completed": "Завершена",
    "cancelled": "Отменена",
}
STATUS_ICONS = {
    "new": "🆕", "under_review": "🔎", "needs_info": "💬", "quote_sent": "💰",
    "awaiting_client": "⏳", "in_progress": "⚙️", "completed": "✅", "cancelled": "🚫",
}


def button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


def main_menu(last_order_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if last_order_id is not None:
        rows.append([button(f"📦 Открыть последний заказ #{last_order_id}", f"my:view:{last_order_id}", "primary")])
    rows.extend([
        [button("🚀 Заказать разработку", "order:new", "primary")],
        [button("🧩 Услуги и цены", "menu:services"), button("📂 Примеры работ", "menu:portfolio")],
        [button("📋 Мои заявки", "my:orders"), button("👨‍💻 Связаться", "menu:contact")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_resume_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("▶️ Продолжить заявку", "start:resume", "primary")],
        [button("🔄 Начать новую", "start:restart")],
        [button("✖️ Отбросить черновик", "start:discard", "danger")],
    ])


def service_menu(services: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows = [[button(service["title"], f"service:{service['id']}")] for service in services]
    rows.append([button("✖️ Отменить заявку", "order:cancel", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_catalog_menu(services: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows = [[button(f"{service['title']}", f"serviceinfo:{service['id']}")] for service in services]
    rows.append([button("🏠 Главное меню", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_detail_menu(service_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🚀 Заказать эту услугу", f"serviceorder:{service_id}", "primary")],
        [button("⬅️ К услугам и ценам", "menu:services")],
        [button("🏠 Главное меню", "menu:home")],
    ])


def customer_notification_menu(
    order_id: int, notification_id: int, *, can_reply_to_clarification: bool = False,
) -> InlineKeyboardMarkup:
    """Keep notifications compact and contextual: at most two primary actions."""
    rows = [[button("📦 Открыть заявку", f"customer:open:{order_id}:{notification_id}", "primary")]]
    if can_reply_to_clarification:
        rows.append([button("💬 Ответить на уточнение", f"customer:clarify:answer:{order_id}", "primary")])
    else:
        rows.append([button("🏠 Главное меню", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def price_note_menu(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Пропустить пояснение", f"admin:price-note-skip:{order_id}", "primary")],
        [button("❌ Отмена", f"admin:view:{order_id}", "danger")],
    ])


def price_confirm_menu(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("✅ Подтвердить цену", f"admin:price-confirm:{order_id}", "success")],
        [button("✏️ Изменить цену", f"admin:price:{order_id}", "primary")],
        [button("❌ Отмена", f"admin:view:{order_id}", "danger")],
    ])


def admin_status_menu(order_id: int, current_status: str) -> InlineKeyboardMarkup:
    targets = sorted(
        ALLOWED_TRANSITIONS.get(current_status, set()),
        key=lambda item: list(STATUS_LABELS).index(item) if item in STATUS_LABELS else 999,
    )
    rows = []
    for target in targets:
        label = STATUS_LABELS.get(target, target)
        style = "success" if target == "completed" else "danger" if target == "cancelled" else "primary" if target in {"under_review", "in_progress"} else None
        rows.append([button(f"{STATUS_ICONS.get(target, '')} {label}", f"status:{order_id}:{target}", style)])
    rows.append([button("⬅️ К заявке", f"admin:view:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def features_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Пропустить", "skip:features")],
        [button("✖️ Отмена", "order:cancel", "danger")],
    ])


def budget_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("До 5 000 ₽", "budget:under_5k"), button("5–15 тыс. ₽", "budget:5_15k")],
        [button("15–30 тыс. ₽", "budget:15_30k"), button("30 тыс. ₽+", "budget:over_30k")],
        [button("Указать свой бюджет", "budget:custom")],
        [button("Пока не знаю / пропустить", "budget:skip")],
        [button("✖️ Отмена", "order:cancel", "danger")],
    ])


def deadline_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Срочно · 1–3 дня", "deadline:urgent")],
        [button("Около недели", "deadline:week"), button("2–4 недели", "deadline:month")],
        [button("Срок гибкий", "deadline:flexible")],
        [button("Пока не знаю / пропустить", "deadline:skip")],
        [button("✖️ Отмена", "order:cancel", "danger")],
    ])


def repository_skip_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Пропустить", "repo:skip")],
        [button("✖️ Отмена", "order:cancel", "danger")],
    ])


def contact_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("💬 Связаться через Telegram", "contact:telegram", "primary")],
        [button("📱 Указать другой контакт", "contact:custom")],
        [button("✖️ Отмена", "order:cancel", "danger")],
    ])


def confirmation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("✅ Отправить заявку", "order:confirm", "success")],
        [button("✏️ Исправить данные", "edit:menu")],
        [button("❌ Отменить заявку", "order:cancel", "danger")],
    ])


def edit_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Услуга", "edit:service"), button("Описание", "edit:description")],
        [button("Функции", "edit:features"), button("Бюджет", "edit:budget")],
        [button("Срок", "edit:deadline"), button("Ссылка на проект", "edit:repo")],
        [button("Контакт", "edit:contact")],
        [button("⬅️ Вернуться к проверке", "edit:back")],
    ])


def my_orders_menu(orders: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for order in orders:
        title = str(order["service_title"])[:24]
        label = STATUS_LABELS.get(order["status"], order["status"])
        rows.append([button(f"#{order['id']} · {label} · {title}", f"my:view:{order['id']}")])
    rows.append([button("🏠 Главное меню", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_order_actions(
    order_id: int, can_cancel: bool, *, has_open_clarification: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if has_open_clarification:
        rows.append([button("💬 Ответить на уточнение", f"customer:clarify:answer:{order_id}", "primary")])
    if can_cancel:
        rows.append([button("❌ Запросить отмену", f"mycancel:ask:{order_id}", "danger")])
    rows.append([button("⬅️ К моим заявкам", "my:orders"), button("🏠 Меню", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_cancel_confirm(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Да, отменить", f"mycancel:confirm:{order_id}", "danger")],
        [button("Нет, оставить", f"my:view:{order_id}")],
    ])


def admin_dashboard_menu() -> InlineKeyboardMarkup:
    """Compact daily-use dashboard; less common filters remain available in the order list."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🆕 Новые заявки", "admin:list:new:0", "primary"), button("📋 Все заявки", "admin:list:all:0")],
        [button("⚙️ В работе", "admin:list:progress:0"), button("💬 Нужно уточнение", "admin:list:needs_info:0")],
        [button("✅ Завершённые", "admin:list:completed:0"), button("🔎 Поиск по номеру", "admin:search")],
        [button("📨 Проблемы уведомлений", "admin:list:notifications:0", "danger")],
        [button("🔁 Повторить недоставленные", "admin:retry-notifications")],
        [button("📊 Обновить панель", "admin:dashboard")],
    ])



def admin_order_list_menu(
    orders: list[dict], *, filter_key: str = "all", page: int = 0, has_next: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for order in orders:
        status = str(order["status"])
        label = STATUS_LABELS.get(status, status)
        title = str(order["service_title"])[:20]
        rows.append([button(f"#{order['id']} · {STATUS_ICONS.get(status, '📄')} {label} · {title}", f"admin:view:{order['id']}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(button("⬅️ Назад", f"admin:list:{filter_key}:{page - 1}"))
    if has_next:
        nav.append(button("Далее ➡️", f"admin:list:{filter_key}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("🛠 Панель администратора", "admin:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_notification_menu(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("📂 Открыть заявку в панели", f"admin:view:{order_id}", "primary")],
        [button("🗑 Убрать это уведомление", f"admin:dismiss:{order_id}", "danger")],
    ])


def admin_order_menu(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows = [
        [button("🔄 Изменить статус", f"admin:status:{order_id}", "primary")],
        [button("💰 Назначить цену", f"admin:price:{order_id}", "success")],
    ]
    if status not in {"completed", "cancelled"}:
        label = "💬 Уточнение ожидает ответа" if status == "needs_info" else "💬 Запросить уточнение"
        callback = f"admin:clarify:view:{order_id}" if status == "needs_info" else f"admin:clarify:start:{order_id}"
        rows.append([button(label, callback)])
    rows.extend([
        [button("🕒 История", f"admin:history:{order_id}"), button("📝 Внутренняя заметка", f"admin:note:{order_id}")],
        [button("✉️ Ответить клиенту", f"admin:reply:{order_id}")],
        [button("⬅️ К заявкам", "admin:list:all:0"), button("🛠 Панель", "admin:dashboard")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_clarification_confirm_menu(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("✅ Отправить уточнение", f"admin:clarify:confirm:{order_id}", "success")],
        [button("❌ Отмена", f"admin:clarify:cancel:{order_id}", "danger")],
    ])


def admin_clarification_view_menu(
    order_id: int, has_open_clarification: bool, *, can_create: bool = True,
) -> InlineKeyboardMarkup:
    rows = []
    if has_open_clarification:
        rows.append([button("📨 Напомнить клиенту", f"admin:clarify:remind:{order_id}", "primary")])
    elif can_create:
        rows.append([button("💬 Создать уточнение", f"admin:clarify:start:{order_id}", "primary")])
    rows.append([button("⬅️ К заявке", f"admin:view:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_clarification_cancel_menu(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("❌ Отменить ответ", f"customer:clarify:cancel:{order_id}", "danger")],
    ])
