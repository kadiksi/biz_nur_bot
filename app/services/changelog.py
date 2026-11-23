from datetime import date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChangeLog
from app.services.access import require_owner
from app.services.context import Actor


def _jsonable(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    return v


def snapshot(obj: Any, fields: list[str]) -> dict[str, Any]:
    return {f: _jsonable(getattr(obj, f)) for f in fields}


async def log(
    session: AsyncSession,
    actor: Actor,
    *,
    entity: str,
    entity_id: int | None,
    action: str,
    summary: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """Добавляет запись в той же транзакции, что и само изменение. Коммитит вызывающий."""
    session.add(
        ChangeLog(
            actor_id=actor.employee_id,
            actor_name=actor.name,
            channel=actor.channel,
            entity=entity,
            entity_id=entity_id,
            action=action,
            summary=summary,
            before=_jsonable(before),
            after=_jsonable(after),
        )
    )


async def recent(session: AsyncSession, actor: Actor, limit: int = 50, entity: str | None = None) -> list[ChangeLog]:
    require_owner(actor, "смотреть журнал изменений")
    q = select(ChangeLog).order_by(ChangeLog.id.desc()).limit(limit)
    if entity:
        q = q.where(ChangeLog.entity == entity)
    return list(await session.scalars(q))
