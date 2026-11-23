"""Сотрудники и права. Менять может только владелец.

Роль «владелец» можно дать только аккаунту OWNER_TELEGRAM_ID, а его нельзя
отключить или понизить — защита от случайной эскалации через бота.
"""
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Company, Department, Employee, EmployeeAccess, Role
from app.services import changelog, refs
from app.services.access import can_read_department, require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import NotFound, ValidationError

ROLE_LABELS = {Role.OWNER: "владелец", Role.HEAD: "руководитель отдела", Role.EMPLOYEE: "сотрудник"}
EMP_FIELDS = ["telegram_id", "full_name", "username", "role", "department_id", "company_id", "is_active"]


def _role(v: Any) -> Role:
    try:
        return Role(v)
    except ValueError:
        raise ValidationError(f"Роль должна быть одной из: {', '.join(r.value for r in Role)}") from None


async def _dept_name(session, dept_id):
    return (await session.get(Department, dept_id)).name if dept_id else "—"


async def _comp_name(session, comp_id):
    return (await session.get(Company, comp_id)).name if comp_id else "все компании"


def _check_role(role: Role, telegram_id: int, department_id: int | None) -> None:
    if role == Role.OWNER and telegram_id != settings.owner_telegram_id:
        raise ValidationError("Роль «владелец» есть только у аккаунта из OWNER_TELEGRAM_ID")
    if role != Role.OWNER and department_id is None:
        raise ValidationError("Руководителю и сотруднику нужно указать отдел")


async def list_employees(
    session: AsyncSession, actor: Actor, department: str | None = None, include_inactive: bool = False
) -> list[Employee]:
    """Владелец видит всех; остальные — активных коллег по своим отделам (например, чтобы назначить карточку)."""
    q = select(Employee).order_by(Employee.full_name)
    if department:
        q = q.where(Employee.department_id == (await refs.get_department(session, department)).id)
    if not (actor.is_owner and include_inactive):
        q = q.where(Employee.is_active.is_(True))
    emps = list(await session.scalars(q))
    if actor.is_owner:
        return emps
    depts = {d.id: d.code for d in await refs.list_departments(session)}
    companies = [c.id for c in await refs.list_companies(session)]

    def visible(e: Employee) -> bool:
        if e.department_id is None:
            return False
        ids = [e.company_id] if e.company_id else companies
        return any(can_read_department(actor, depts[e.department_id], cid) for cid in ids)

    return [e for e in emps if visible(e)]


async def _department_id(session, department) -> int | None:
    return None if department is None else (await refs.get_department(session, department)).id


async def _company_id(session, company) -> int | None:
    return None if company in (None, refs.HOLDING) else (await refs.get_company(session, company)).id


@write_op("employees.create")
async def create_employee(
    session: AsyncSession,
    actor: Actor,
    *,
    telegram_id: int,
    full_name: str,
    role: str = Role.EMPLOYEE.value,
    department: str | None = None,
    company: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
) -> ChangePreview:
    require_owner(actor, "добавлять сотрудников")
    full_name = (full_name or "").strip()
    if not full_name:
        raise ValidationError("Укажите имя сотрудника")
    telegram_id = int(telegram_id)
    if await session.scalar(select(Employee.id).where(Employee.telegram_id == telegram_id)):
        raise ValidationError(f"Сотрудник с Telegram ID {telegram_id} уже есть")
    r = _role(role)
    dept_id, comp_id = await _department_id(session, department), await _company_id(session, company)
    _check_role(r, telegram_id, dept_id)
    preview = ChangePreview(
        title="Новый сотрудник",
        changes=[
            FieldChange("Имя", None, full_name),
            FieldChange("Telegram ID", None, str(telegram_id)),
            FieldChange("Роль", None, ROLE_LABELS[r]),
            FieldChange("Отдел", None, await _dept_name(session, dept_id)),
            FieldChange("Компания", None, await _comp_name(session, comp_id)),
        ],
    )
    if dry_run:
        return preview
    emp = Employee(telegram_id=telegram_id, full_name=full_name, username=username, role=r,
                   department_id=dept_id, company_id=comp_id, is_active=True)
    session.add(emp)
    await session.flush()
    await changelog.log(session, actor, entity="employee", entity_id=emp.id, action="create",
                        summary=preview.render(), after=changelog.snapshot(emp, EMP_FIELDS))
    await session.commit()
    preview.entity_id = emp.id
    return preview


@write_op("employees.update")
async def update_employee(
    session: AsyncSession,
    actor: Actor,
    *,
    employee_id: int,
    full_name: str = UNSET,
    username: str | None = UNSET,
    role: str = UNSET,
    department: str | None = UNSET,
    company: str | None = UNSET,
    is_active: bool = UNSET,
    dry_run: bool = False,
) -> ChangePreview:
    require_owner(actor, "менять сотрудников")
    emp = await refs.get_employee(session, employee_id)
    is_main_owner = emp.telegram_id == settings.owner_telegram_id

    new: dict[str, Any] = {}
    changes: list[FieldChange] = []
    if full_name is not UNSET and full_name.strip() and full_name.strip() != emp.full_name:
        new["full_name"] = full_name.strip()
        changes.append(FieldChange("Имя", emp.full_name, new["full_name"]))
    if username is not UNSET and username != emp.username:
        new["username"] = username
        changes.append(FieldChange("Username", emp.username or "—", username or "—"))
    if role is not UNSET and _role(role) != emp.role:
        if is_main_owner:
            raise ValidationError("Роль владельца из OWNER_TELEGRAM_ID изменить нельзя")
        new["role"] = _role(role)
        changes.append(FieldChange("Роль", ROLE_LABELS[emp.role], ROLE_LABELS[new["role"]]))
    if department is not UNSET:
        dept_id = await _department_id(session, department)
        if dept_id != emp.department_id:
            new["department_id"] = dept_id
            changes.append(FieldChange("Отдел", await _dept_name(session, emp.department_id), await _dept_name(session, dept_id)))
    if company is not UNSET:
        comp_id = await _company_id(session, company)
        if comp_id != emp.company_id:
            new["company_id"] = comp_id
            changes.append(FieldChange("Компания", await _comp_name(session, emp.company_id), await _comp_name(session, comp_id)))
    if is_active is not UNSET and bool(is_active) != emp.is_active:
        if is_main_owner and not is_active:
            raise ValidationError("Владельца из OWNER_TELEGRAM_ID отключить нельзя")
        new["is_active"] = bool(is_active)
        changes.append(FieldChange("Доступ", "активен" if emp.is_active else "отключён", "активен" if is_active else "отключён"))
    if not changes:
        raise ValidationError("Нечего менять: новые значения совпадают с текущими")
    _check_role(new.get("role", emp.role), emp.telegram_id, new.get("department_id", emp.department_id))

    preview = ChangePreview(title=f"Изменение сотрудника {emp.full_name}", changes=changes)
    if dry_run:
        return preview
    before = changelog.snapshot(emp, EMP_FIELDS)
    for k, v in new.items():
        setattr(emp, k, v)
    await changelog.log(session, actor, entity="employee", entity_id=emp.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(emp, EMP_FIELDS))
    await session.commit()
    preview.entity_id = emp.id
    return preview


async def list_access(session: AsyncSession, actor: Actor, employee_id: int) -> list[EmployeeAccess]:
    require_owner(actor, "смотреть права сотрудников")
    return list(await session.scalars(select(EmployeeAccess).where(EmployeeAccess.employee_id == employee_id)))


@write_op("employees.grant_access")
async def grant_access(
    session: AsyncSession,
    actor: Actor,
    *,
    employee_id: int,
    department: str,
    company: str | None = None,
    can_write_kanban: bool = False,
    dry_run: bool = False,
) -> ChangePreview:
    require_owner(actor, "выдавать доступ")
    emp = await refs.get_employee(session, employee_id)
    dept_id, comp_id = await _department_id(session, department), await _company_id(session, company)
    existing = await session.scalar(
        select(EmployeeAccess).where(
            EmployeeAccess.employee_id == emp.id,
            EmployeeAccess.department_id == dept_id,
            EmployeeAccess.company_id.is_(None) if comp_id is None else EmployeeAccess.company_id == comp_id,
        )
    )
    scope = f"{await _dept_name(session, dept_id)} · {await _comp_name(session, comp_id)}"
    level = "чтение + канбан" if can_write_kanban else "только чтение"
    if existing and existing.can_write_kanban == bool(can_write_kanban):
        raise ValidationError(f"У {emp.full_name} уже есть такой доступ: {scope}, {level}")
    before_level = None if existing is None else ("чтение + канбан" if existing.can_write_kanban else "только чтение")
    preview = ChangePreview(
        title=f"Доступ для {emp.full_name}",
        changes=[FieldChange(scope, before_level, level)],
    )
    if dry_run:
        return preview
    if existing is None:
        existing = EmployeeAccess(employee_id=emp.id, department_id=dept_id, company_id=comp_id,
                                  can_write_kanban=bool(can_write_kanban))
        session.add(existing)
    else:
        existing.can_write_kanban = bool(can_write_kanban)
    await session.flush()
    await changelog.log(session, actor, entity="employee_access", entity_id=existing.id, action="grant",
                        summary=preview.render(),
                        after={"employee_id": emp.id, "department_id": dept_id, "company_id": comp_id,
                               "can_write_kanban": bool(can_write_kanban)})
    await session.commit()
    preview.entity_id = existing.id
    return preview


@write_op("employees.revoke_access")
async def revoke_access(session: AsyncSession, actor: Actor, *, access_id: int, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "отзывать доступ")
    acc = await session.get(EmployeeAccess, access_id)
    if acc is None:
        raise NotFound(f"Доступ #{access_id} не найден")
    emp = await refs.get_employee(session, acc.employee_id)
    scope = f"{await _dept_name(session, acc.department_id)} · {await _comp_name(session, acc.company_id)}"
    preview = ChangePreview(title=f"Отзыв доступа у {emp.full_name}", changes=[FieldChange(scope, "есть", None)])
    if dry_run:
        return preview
    before = changelog.snapshot(acc, ["employee_id", "department_id", "company_id", "can_write_kanban"])
    await session.delete(acc)
    await changelog.log(session, actor, entity="employee_access", entity_id=access_id, action="revoke",
                        summary=preview.render(), before=before)
    await session.commit()
    preview.entity_id = access_id
    return preview
