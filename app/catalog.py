"""Configurable BOTVEX services catalog."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVICES: list[dict[str, str]] = [
    {
        "id": "telegram_bot",
        "title": "🤖 Telegram-бот под заказ",
        "description": "Бот для заявок, записи, заказов, поддержки или внутренних процессов.",
        "price": "Индивидуальная оценка после обсуждения требований",
    },
    {
        "id": "automation",
        "title": "⚙️ Python-автоматизация",
        "description": "Автоматизация повторяющихся действий, обработки данных и рабочих процессов.",
        "price": "Зависит от объёма и источников данных",
    },
    {
        "id": "api",
        "title": "🔌 API и интеграции",
        "description": "Связь Telegram-бота с CRM, таблицами, сервисами и внешними API.",
        "price": "После оценки документации и требований",
    },
    {
        "id": "ai",
        "title": "🧠 ИИ-интеграция",
        "description": "ИИ-консультант, поиск по базе знаний, классификация и обработка обращений.",
        "price": "Отдельно учитываются расходы на API моделей",
    },
    {
        "id": "fixes",
        "title": "🛠 Исправление или доработка",
        "description": "Разбор ошибок, улучшение существующего Python-кода или Telegram-бота.",
        "price": "После диагностики проекта",
    },
    {
        "id": "hosting",
        "title": "🖥 Развёртывание и поддержка",
        "description": "Настройка запуска, логирования, резервных копий и технического обслуживания.",
        "price": "Зависит от инфраструктуры и объёма поддержки",
    },
    {
        "id": "custom",
        "title": "✨ Другая задача",
        "description": "Опиши результат, который хочешь получить.",
        "price": "Обсудим подход и оценку",
    },
]


def load_services(path: str | Path | None = None) -> list[dict[str, str]]:
    """Load service definitions from JSON; fall back to safe defaults if unavailable."""
    candidate = Path(path) if path else Path("config/services.json")
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        raw: Any = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError("services JSON must contain a non-empty list")
        services: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each service must be an object")
            sid = str(item.get("id", "")).strip()
            title = str(item.get("title", "")).strip()
            description = str(item.get("description", "")).strip()
            price = str(item.get("price", "Уточняется")).strip()
            if not sid or not sid.replace("_", "").isalnum() or sid in seen or not title or not description:
                raise ValueError("service needs a unique simple id, title and description")
            if len(sid) > 32 or len(title) > 100 or len(description) > 500 or len(price) > 200:
                raise ValueError(f"service fields exceed the allowed length: {sid}")
            seen.add(sid)
            services.append({"id": sid, "title": title, "description": description, "price": price})
        return services
    except FileNotFoundError:
        return [dict(item) for item in DEFAULT_SERVICES]
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid services catalog at {candidate}: {exc}") from exc


def service_by_id(services: list[dict[str, str]], service_id: str) -> dict[str, str] | None:
    return next((service for service in services if service["id"] == service_id), None)
