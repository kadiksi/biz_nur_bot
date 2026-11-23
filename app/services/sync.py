"""Свежесть данных (sync_status) для метки на каждом разделе."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SyncState, SyncStatus
from app.services import changelog, refs
from app.services.access import can_read_department, can_read_summary, require, require_owner
from app.services.changes import ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.timeutil import plural, to_local, utcnow


@dataclass
class Freshness:
    last_success_at: datetime | None  # самая старая успешная синхронизация в выборке
    has_error: bool
    message: str | None
    label: str
    is_stale: bool


def humanize_age(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "нет данных"
    now = now or utcnow()
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "только что"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    local_dt, local_now = to_local(dt), to_local(now)
    if (local_now.date() - local_dt.date()).days == 1:
        return f"вчера в {local_dt:%H:%M}"
    days = (local_now.date() - local_dt.date()).days
    return f"{days} {plural(days, 'день', 'дня', 'дней')} назад"


async def freshness(
    session: AsyncSession, actor: Actor, company: str, department: str | None = None, now: datetime | None = None
) -> Freshness:
    """department=None — по всем отделам (для Сводки). Для холдинга берётся худшая компания."""
    companies = await refs.resolve_companies(session, company)
    q = select(SyncStatus).where(SyncStatus.company_id.in_([c.id for c in companies]))
    if department:
        dept = await refs.get_department(session, department)
        for c in companies:
            require(can_read_department(actor, dept.code, c.id), f"Нет доступа к разделу «{dept.name}»")
        q = q.where(SyncStatus.department_id == dept.id)
    else:
        for c in companies:
            require(can_read_summary(actor, c.id), f"Нет доступа к данным компании {c.name}")
    rows = list(await session.scalars(q))
    now = now or utcnow()
    if not rows:
        return Freshness(None, False, None, "нет данных о синхронизации", True)
    files = [r for r in rows if r.source == "file"]  # цифры из файла владельца: сами не обновляются
    rows = [r for r in rows if r.source != "file"]
    if not rows:
        last = max(r.last_success_at for r in files)
        return Freshness(last, False, None, f"из файла · загружено {humanize_age(last, now)}", False)

    times = [r.last_success_at for r in rows]
    oldest = None if any(t is None for t in times) else min(times)
    errors = [r for r in rows if r.status == SyncState.ERROR]
    stale = oldest is None or now - oldest > timedelta(hours=settings.sync_stale_hours)
    label = f"обновлено {humanize_age(oldest, now)}" if oldest else "ещё не синхронизировано"
    if errors:
        label += " · ошибка синхронизации"
    return Freshness(oldest, bool(errors), errors[0].message if errors else None, label, stale)


@write_op("sync.record")
async def record_sync(
    session: AsyncSession,
    actor: Actor,
    *,
    source: str,
    company: str,
    department: str,
    ok: bool,
    message: str | None = None,
    dry_run: bool = False,
) -> ChangePreview:
    """Вызывается загрузчиком из 1С (под Actor.system)."""
    require_owner(actor, "отмечать синхронизацию")
    comp = await refs.get_company(session, company)
    dept = await refs.get_department(session, department)
    row = await session.scalar(
        select(SyncStatus).where(
            SyncStatus.source == source, SyncStatus.company_id == comp.id, SyncStatus.department_id == dept.id
        )
    )
    new_state = SyncState.OK if ok else SyncState.ERROR
    preview = ChangePreview(
        title=f"Синхронизация {source} · {comp.name} · {dept.name}",
        changes=[FieldChange("Статус", row.status.value if row else None, new_state.value)],
    )
    if dry_run:
        return preview
    now = utcnow()
    before = changelog.snapshot(row, ["status", "last_success_at", "message"]) if row else None
    if row is None:
        row = SyncStatus(source=source, company_id=comp.id, department_id=dept.id)
        session.add(row)
    row.status, row.last_attempt_at, row.message = new_state, now, message
    if ok:
        row.last_success_at = now
    await session.flush()
    await changelog.log(session, actor, entity="sync_status", entity_id=row.id, action="record",
                        summary=preview.render(), before=before,
                        after=changelog.snapshot(row, ["status", "last_success_at", "message"]))
    await session.commit()
    preview.entity_id = row.id
    return preview
