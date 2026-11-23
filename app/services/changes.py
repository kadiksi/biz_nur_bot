"""Единый механизм записи: preview (ничего не пишет) → apply (пишет + change_log).

Каждая записывающая функция помечена @write_op("имя") и принимает dry_run.
Боты: preview(op, params) → показать render() → по «Подтвердить» apply(op, params).
Сайт может вызывать функции напрямую.
"""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context import Actor
from app.services.errors import ValidationError


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()  # «поле не передано» — отличается от None («очистить»)


@dataclass
class FieldChange:
    label: str
    before: str | None  # None — поля не было (создание)
    after: str | None   # None — поле удаляется


@dataclass
class ChangePreview:
    title: str
    changes: list[FieldChange] = field(default_factory=list)
    op: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    entity_id: int | None = None

    def render(self) -> str:
        lines = [self.title]
        for c in self.changes:
            if c.before is None:
                lines.append(f"• {c.label}: {c.after}")
            elif c.after is None:
                lines.append(f"• {c.label}: {c.before} → (удалено)")
            else:
                lines.append(f"• {c.label}: {c.before} → {c.after}")
        return "\n".join(lines)


WriteFn = Callable[..., Awaitable[ChangePreview]]
_REGISTRY: dict[str, WriteFn] = {}
_EXTERNAL: set[str] = set()


def write_op(name: str, *, external: bool = False) -> Callable[[WriteFn], WriteFn]:
    """external=True — применение что-то делает вне нашей базы (запрос в сервис, загрузка из 1С):
    в песочнице такие операции только проверяются."""
    if external:
        _EXTERNAL.add(name)

    def deco(fn: WriteFn) -> WriteFn:
        @functools.wraps(fn)
        async def wrapper(session: AsyncSession, actor: Actor, *, dry_run: bool = False, **params) -> ChangePreview:
            result = await fn(session, actor, dry_run=dry_run, **params)
            result.op = name
            result.params = params
            result.applied = not dry_run
            return result

        _REGISTRY[name] = wrapper
        return wrapper

    return deco


def is_external(op: str) -> bool:
    return op in _EXTERNAL


def list_ops() -> list[str]:
    return sorted(_REGISTRY)


def _get(op: str) -> WriteFn:
    try:
        return _REGISTRY[op]
    except KeyError:
        raise ValidationError(f"Неизвестная операция «{op}»") from None


async def _call(session, actor, op, params, dry_run):
    fn = _get(op)
    try:
        return await fn(session, actor, dry_run=dry_run, **params)
    except TypeError as e:
        raise ValidationError(f"Неверные параметры для «{op}»: {e}") from None


async def preview(session: AsyncSession, actor: Actor, op: str, params: dict[str, Any]) -> ChangePreview:
    return await _call(session, actor, op, params, dry_run=True)


async def apply(session: AsyncSession, actor: Actor, op: str, params: dict[str, Any]) -> ChangePreview:
    return await _call(session, actor, op, params, dry_run=False)
