"""Ожидающие подтверждения изменения. Не зависит от Telegram — тестируется отдельно."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import changes
from app.services.changes import ChangePreview
from app.services.context import Actor
from app.services.sandbox import Sandbox
from app.services.errors import ServiceError
from app.timeutil import utcnow

TTL = timedelta(hours=1)


@dataclass
class PendingBatch:
    id: str
    chat_id: int
    previews: list[ChangePreview]
    created_at: datetime = field(default_factory=utcnow)

    def expired(self) -> bool:
        return utcnow() - self.created_at > TTL


class PendingStore:
    def __init__(self) -> None:
        self._items: dict[str, PendingBatch] = {}

    def add(self, chat_id: int, previews: list[ChangePreview]) -> PendingBatch:
        self._gc()
        batch = PendingBatch(secrets.token_hex(6), chat_id, previews)
        self._items[batch.id] = batch
        return batch

    def pop(self, batch_id: str, chat_id: int) -> PendingBatch | None:
        batch = self._items.pop(batch_id, None)
        if batch is None or batch.chat_id != chat_id or batch.expired():
            return None
        return batch

    def _gc(self) -> None:
        for k in [k for k, b in self._items.items() if b.expired()]:
            del self._items[k]


def render_batch(previews: list[ChangePreview]) -> str:
    if len(previews) == 1:
        return "Подтвердите изменение:\n\n" + previews[0].render()
    body = "\n\n".join(f"{i}. {p.render()}" for i, p in enumerate(previews, 1))
    return f"Подтвердите изменения ({len(previews)}):\n\n{body}"


@dataclass
class ConfirmResult:
    applied: list[ChangePreview] = field(default_factory=list)
    errors: list[tuple[ChangePreview, str]] = field(default_factory=list)
    changed: list[ChangePreview] | None = None  # данные изменились — нужен новый предпросмотр

    def summary(self) -> str:
        lines = []
        if self.applied:
            lines.append("Применено:")
            lines += [f"• {p.render().splitlines()[0]}" for p in self.applied]
        if self.errors:
            lines.append("Не применено:")
            lines += [f"• {p.render().splitlines()[0]} — {err}" for p, err in self.errors]
        return "\n".join(lines)


async def confirm(session: AsyncSession, actor: Actor, batch: PendingBatch) -> ConfirmResult:
    """Перед записью ещё раз строит предпросмотр. Если он отличается от показанного
    (кто-то успел изменить данные), ничего не применяет и возвращает новый."""
    fresh: list[ChangePreview] = []
    async with Sandbox(session) as sandbox:  # цепочкой: показатель видит отдел из того же пакета
        for p in batch.previews:
            try:
                fresh.append(await sandbox.step(actor, p.op, p.params))
            except ServiceError as e:
                return ConfirmResult(errors=[(p, str(e))])
    if any(a.render() != b.render() for a, b in zip(batch.previews, fresh, strict=True)):
        return ConfirmResult(changed=fresh)

    result = ConfirmResult()
    for p in batch.previews:
        try:
            result.applied.append(await changes.apply(session, actor, p.op, p.params))
        except ServiceError as e:
            await session.rollback()
            result.errors.append((p, str(e)))
    return result
