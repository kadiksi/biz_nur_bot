"""Канбан: Бэклог → В работе → На проверке → Готово.

Сотрудник создаёт и двигает карточки своего отдела, правит свои/назначенные.
Руководитель — всё в своём отделе, включая удаление. Владелец — везде.
"""
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CardStatus, Channel, Company, Department, Employee, KanbanCard
from app.services import changelog, refs
from app.services.access import (
    can_delete_card,
    can_edit_card,
    can_read_department,
    can_write_kanban,
    require,
)
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor, actor_from_employee
from app.services.errors import NotFound, ValidationError
from app.services.format import fmt_date

STATUS_LABELS = {
    CardStatus.BACKLOG: "Бэклог",
    CardStatus.IN_PROGRESS: "В работе",
    CardStatus.REVIEW: "На проверке",
    CardStatus.DONE: "Готово",
}
PRIORITY_LABELS = {"low": "низкий", "normal": "обычный", "high": "высокий"}
CARD_FIELDS = ["company_id", "department_id", "title", "description", "status", "position",
               "priority", "assignee_id", "due_date", "recurring_task_id"]


# ---------- валидация ----------


def _status(v: Any) -> CardStatus:
    try:
        return CardStatus(v)
    except ValueError:
        raise ValidationError(f"Неизвестная колонка «{v}». Допустимо: {', '.join(s.value for s in CardStatus)}") from None


def _priority(v: Any) -> str:
    if v not in PRIORITY_LABELS:
        raise ValidationError(f"Приоритет должен быть одним из: {', '.join(PRIORITY_LABELS)}")
    return v


def _title(v: Any) -> str:
    v = (v or "").strip()
    if not v:
        raise ValidationError("Название карточки не может быть пустым")
    if len(v) > 200:
        raise ValidationError("Название карточки длиннее 200 символов")
    return v


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise ValidationError(f"Дата «{v}» не распознана, нужен формат ГГГГ-ММ-ДД") from None


async def _assignee(session: AsyncSession, assignee_id: int | None, dept: Department, company_id: int) -> Employee | None:
    if assignee_id is None:
        return None
    emp = await refs.get_employee(session, assignee_id)
    if not emp.is_active:
        raise ValidationError(f"{emp.full_name} отключён(а), назначить нельзя")
    a = await actor_from_employee(session, emp, Channel.SCRIPT)  # только для проверки прав
    if not can_read_department(a, dept.code, company_id):
        raise ValidationError(f"{emp.full_name} не работает в отделе «{dept.name}» этой компании")
    return emp


async def _emp_name(session: AsyncSession, emp_id: int | None) -> str:
    if emp_id is None:
        return "не назначен"
    emp = await session.get(Employee, emp_id)
    return emp.full_name if emp else f"#{emp_id}"


async def _load(session: AsyncSession, card_id: int) -> tuple[KanbanCard, Department, Company]:
    card = await session.get(KanbanCard, card_id)
    if card is None:
        raise NotFound(f"Карточка #{card_id} не найдена")
    dept = await session.get(Department, card.department_id)
    comp = await session.get(Company, card.company_id)
    return card, dept, comp


async def _column(session: AsyncSession, company_id: int, department_id: int, status: CardStatus) -> list[KanbanCard]:
    return list(
        await session.scalars(
            select(KanbanCard)
            .where(
                KanbanCard.company_id == company_id,
                KanbanCard.department_id == department_id,
                KanbanCard.status == status,
            )
            .order_by(KanbanCard.position, KanbanCard.id)
        )
    )


# ---------- чтение ----------


async def board(session: AsyncSession, actor: Actor, company: str, department: str) -> dict[CardStatus, list[KanbanCard]]:
    dept = await refs.get_department(session, department)
    companies = await refs.resolve_companies(session, company)
    for c in companies:
        require(can_read_department(actor, dept.code, c.id), f"Нет доступа к канбану «{dept.name}» компании {c.name}")
    cards = await session.scalars(
        select(KanbanCard)
        .where(KanbanCard.company_id.in_([c.id for c in companies]), KanbanCard.department_id == dept.id)
        .order_by(KanbanCard.position, KanbanCard.id)
    )
    out: dict[CardStatus, list[KanbanCard]] = {s: [] for s in CardStatus}
    for card in cards:
        out[card.status].append(card)
    return out


async def get_card(session: AsyncSession, actor: Actor, card_id: int) -> KanbanCard:
    card, dept, comp = await _load(session, card_id)
    require(can_read_department(actor, dept.code, comp.id), "Нет доступа к этой карточке")
    return card


# ---------- запись ----------


@write_op("kanban.create_card")
async def create_card(
    session: AsyncSession,
    actor: Actor,
    *,
    company: str,
    department: str,
    title: str,
    description: str | None = None,
    assignee_id: int | None = None,
    priority: str = "normal",
    due_date: str | date | None = None,
    status: str = CardStatus.BACKLOG.value,
    recurring_task_id: int | None = None,
    dry_run: bool = False,
) -> ChangePreview:
    comp = await refs.get_company(session, company)
    dept = await refs.get_department(session, department)
    require(can_write_kanban(actor, dept.code, comp.id), f"Нет прав создавать карточки в «{dept.name}» компании {comp.name}")
    title, st, priority, due = _title(title), _status(status), _priority(priority), _date(due_date)
    await _assignee(session, assignee_id, dept, comp.id)

    changes = [
        FieldChange("Название", None, title),
        FieldChange("Колонка", None, STATUS_LABELS[st]),
        FieldChange("Исполнитель", None, await _emp_name(session, assignee_id)),
        FieldChange("Приоритет", None, PRIORITY_LABELS[priority]),
    ]
    if due:
        changes.append(FieldChange("Срок", None, fmt_date(due)))
    if description:
        changes.append(FieldChange("Описание", None, description))
    preview = ChangePreview(title=f"Новая карточка · {comp.name} · {dept.name}", changes=changes)
    if dry_run:
        return preview

    max_pos = await session.scalar(
        select(func.max(KanbanCard.position)).where(
            KanbanCard.company_id == comp.id, KanbanCard.department_id == dept.id, KanbanCard.status == st
        )
    )
    card = KanbanCard(
        company_id=comp.id,
        department_id=dept.id,
        title=title,
        description=description,
        status=st,
        position=(max_pos + 1) if max_pos is not None else 0,
        priority=priority,
        assignee_id=assignee_id,
        created_by_id=actor.employee_id,
        due_date=due,
        recurring_task_id=recurring_task_id,
    )
    session.add(card)
    await session.flush()
    await changelog.log(session, actor, entity="kanban_card", entity_id=card.id, action="create",
                        summary=preview.render(), after=changelog.snapshot(card, CARD_FIELDS))
    await session.commit()
    preview.entity_id = card.id
    return preview


@write_op("kanban.move_card")
async def move_card(
    session: AsyncSession,
    actor: Actor,
    *,
    card_id: int,
    status: str,
    position: int | None = None,
    dry_run: bool = False,
) -> ChangePreview:
    """position — индекс в целевой колонке (0 — наверх); None — в конец."""
    card, dept, comp = await _load(session, card_id)
    require(can_write_kanban(actor, dept.code, comp.id), "Нет прав двигать карточки этого отдела")
    st = _status(status)
    column = [c for c in await _column(session, comp.id, dept.id, st) if c.id != card.id]
    idx = len(column) if position is None else max(0, min(int(position), len(column)))

    changes = []
    if st != card.status:
        changes.append(FieldChange("Колонка", STATUS_LABELS[card.status], STATUS_LABELS[st]))
    old_idx = None
    if st == card.status:
        old_idx = [c.id for c in await _column(session, comp.id, dept.id, st)].index(card.id)
        if old_idx != idx:
            changes.append(FieldChange("Позиция", str(old_idx + 1), str(idx + 1)))
    preview = ChangePreview(title=f"Перемещение карточки «{card.title}»", changes=changes)
    if dry_run or not changes:
        return preview

    before = {"status": card.status, "position": card.position}
    column.insert(idx, card)
    card.status = st
    for i, c in enumerate(column):
        c.position = i
    await changelog.log(session, actor, entity="kanban_card", entity_id=card.id, action="move",
                        summary=preview.render(), before=before,
                        after={"status": card.status, "position": card.position})
    await session.commit()
    preview.entity_id = card.id
    return preview


@write_op("kanban.update_card")
async def update_card(
    session: AsyncSession,
    actor: Actor,
    *,
    card_id: int,
    title: str = UNSET,
    description: str | None = UNSET,
    assignee_id: int | None = UNSET,
    priority: str = UNSET,
    due_date: str | date | None = UNSET,
    dry_run: bool = False,
) -> ChangePreview:
    card, dept, comp = await _load(session, card_id)
    require(can_edit_card(actor, card, dept.code), "Нет прав редактировать эту карточку")

    new: dict[str, Any] = {}
    changes: list[FieldChange] = []
    if title is not UNSET and _title(title) != card.title:
        new["title"] = _title(title)
        changes.append(FieldChange("Название", card.title, new["title"]))
    if description is not UNSET and (description or None) != card.description:
        new["description"] = description or None
        changes.append(FieldChange("Описание", card.description or "—", new["description"] or "—"))
    if assignee_id is not UNSET and assignee_id != card.assignee_id:
        await _assignee(session, assignee_id, dept, comp.id)
        new["assignee_id"] = assignee_id
        changes.append(FieldChange("Исполнитель", await _emp_name(session, card.assignee_id),
                                   await _emp_name(session, assignee_id)))
    if priority is not UNSET and _priority(priority) != card.priority:
        new["priority"] = priority
        changes.append(FieldChange("Приоритет", PRIORITY_LABELS[card.priority], PRIORITY_LABELS[priority]))
    if due_date is not UNSET and _date(due_date) != card.due_date:
        new["due_date"] = _date(due_date)
        changes.append(FieldChange("Срок", fmt_date(card.due_date), fmt_date(new["due_date"])))
    if not changes:
        raise ValidationError("Нечего менять: новые значения совпадают с текущими")

    preview = ChangePreview(title=f"Изменение карточки «{card.title}» · {comp.name} · {dept.name}", changes=changes)
    if dry_run:
        return preview
    before = changelog.snapshot(card, list(new))
    for k, v in new.items():
        setattr(card, k, v)
    await changelog.log(session, actor, entity="kanban_card", entity_id=card.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(card, list(new)))
    await session.commit()
    preview.entity_id = card.id
    return preview


@write_op("kanban.delete_card")
async def delete_card(session: AsyncSession, actor: Actor, *, card_id: int, dry_run: bool = False) -> ChangePreview:
    card, dept, comp = await _load(session, card_id)
    require(can_delete_card(actor, dept.code, comp.id), "Удалять карточки может руководитель отдела или владелец")
    preview = ChangePreview(
        title=f"Удаление карточки · {comp.name} · {dept.name}",
        changes=[FieldChange("Карточка", f"«{card.title}» ({STATUS_LABELS[card.status]})", None)],
    )
    if dry_run:
        return preview
    before = changelog.snapshot(card, CARD_FIELDS)
    await session.delete(card)
    await changelog.log(session, actor, entity="kanban_card", entity_id=card_id, action="delete",
                        summary=preview.render(), before=before)
    await session.commit()
    preview.entity_id = card_id
    return preview
