"""Компании холдинга. Список ведёт владелец через главного бота; в коде компании не прописаны."""
import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (Company, Employee, EmployeeAccess, KanbanCard, MetricValue, OneCSource, RecurringTask,
                        SyncStatus)
from app.services import changelog, refs
from app.services.access import require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import ValidationError

CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
_FIELDS = ["code", "name", "sort_order"]


def _name(v) -> str:
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > 60:
        raise ValidationError("Название компании — непустой текст до 60 символов")
    return v.strip()


@write_op("companies.create")
async def create_company(session: AsyncSession, actor: Actor, *, code: str, name: str,
                         sort_order: int | None = None, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "добавлять компании")
    code = (code or "").strip().lower()
    if not CODE_RE.match(code) or code == refs.HOLDING:
        raise ValidationError("Код компании — латиница в нижнем регистре, цифры и дефис, 2–20 символов "
                              "(не holding): например, alpha")
    name = _name(name)
    if await session.scalar(select(Company).where(Company.code == code)):
        raise ValidationError(f"Компания с кодом «{code}» уже есть")
    if await session.scalar(select(Company).where(Company.name == name)):
        raise ValidationError(f"Компания «{name}» уже есть")
    if sort_order is None:
        sort_order = (await session.scalar(select(func.max(Company.sort_order))) or -1) + 1
    preview = ChangePreview(title=f"Новая компания «{name}»",
                            changes=[FieldChange("Код", None, code), FieldChange("Название", None, name)])
    if dry_run:
        return preview
    comp = Company(code=code, name=name, sort_order=int(sort_order))
    session.add(comp)
    await session.flush()
    await changelog.log(session, actor, entity="company", entity_id=comp.id, action="create",
                        summary=preview.render(), after=changelog.snapshot(comp, _FIELDS))
    await session.commit()
    preview.entity_id = comp.id
    return preview


@write_op("companies.update")
async def update_company(session: AsyncSession, actor: Actor, *, code: str, name: str = UNSET,
                         sort_order: int = UNSET, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "менять компании")
    comp = await refs.get_company(session, code)
    diff = []
    if name is not UNSET and _name(name) != comp.name:
        name = _name(name)
        if await session.scalar(select(Company).where(Company.name == name, Company.id != comp.id)):
            raise ValidationError(f"Компания «{name}» уже есть")
        diff.append(FieldChange("Название", comp.name, name))
    if sort_order is not UNSET and int(sort_order) != comp.sort_order:
        diff.append(FieldChange("Место в списке", str(comp.sort_order), str(int(sort_order))))
    if not diff:
        raise ValidationError("Менять нечего")
    preview = ChangePreview(title=f"Компания «{comp.name}»", changes=diff)
    if dry_run:
        return preview
    before = changelog.snapshot(comp, _FIELDS)
    if name is not UNSET:
        comp.name = name
    if sort_order is not UNSET:
        comp.sort_order = int(sort_order)
    await changelog.log(session, actor, entity="company", entity_id=comp.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(comp, _FIELDS))
    await session.commit()
    return preview


@write_op("companies.delete")
async def delete_company(session: AsyncSession, actor: Actor, *, code: str, dry_run: bool = False) -> ChangePreview:
    """Удалить можно только компанию без данных — чтобы случайно не стереть историю."""
    require_owner(actor, "удалять компании")
    comp = await refs.get_company(session, code)
    used = []
    for model, label in ((MetricValue, "планы и факты"), (KanbanCard, "карточки канбана"),
                         (Employee, "сотрудники"), (EmployeeAccess, "доступы сотрудников"),
                         (RecurringTask, "регулярные задачи"), (OneCSource, "источники 1С"),
                         (SyncStatus, "метки синхронизации")):
        if await session.scalar(select(func.count()).select_from(model).where(model.company_id == comp.id)):
            used.append(label)
    if used:
        raise ValidationError(f"У компании «{comp.name}» есть данные ({', '.join(used)}) — удалить нельзя")
    preview = ChangePreview(title=f"Удалить компанию «{comp.name}»", changes=[FieldChange("Код", comp.code, None)])
    if dry_run:
        return preview
    await changelog.log(session, actor, entity="company", entity_id=comp.id, action="delete",
                        summary=preview.render(), before=changelog.snapshot(comp, _FIELDS))
    await session.delete(comp)
    await session.commit()
    return preview
