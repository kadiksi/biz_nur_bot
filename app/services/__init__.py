"""Общий слой: сайт и боты читают и пишут только через него. Права проверяются здесь же.

Импорт модулей регистрирует операции записи в app.services.changes.
"""
from app.services import (  # noqa: F401
    changelog,
    changes,
    companies,
    connectors,
    employees,
    facts,
    kanban,
    metrics,
    onec,
    plans,
    recurring,
    refs,
    secrets,
    site,
    sitecfg,
    structure,
    sync,
    webauth,
)
from app.services.context import Actor, load_actor  # noqa: F401
from app.services.errors import NotFound, PermissionDenied, ServiceError, ValidationError  # noqa: F401
from app.services.periods import Period  # noqa: F401
