"""Actor — кто выполняет действие и через какой канал. Первый аргумент всех функций services."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Department, Employee, EmployeeAccess, Role
from app.services.errors import PermissionDenied


@dataclass(frozen=True)
class Grant:
    department: str
    company_id: int | None  # None — все компании
    can_write_kanban: bool = False


@dataclass(frozen=True)
class Actor:
    channel: Channel
    role: Role
    employee_id: int | None = None
    name: str = "system"
    department: str | None = None
    company_id: int | None = None  # None — все компании
    grants: tuple[Grant, ...] = ()

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER

    @property
    def is_head(self) -> bool:
        return self.role == Role.HEAD

    @classmethod
    def system(cls, channel: Channel) -> Actor:
        """Для планировщика и скриптов загрузки: полные права, без сотрудника."""
        return cls(channel=channel, role=Role.OWNER, name=f"system:{channel.value}")


async def actor_from_employee(session: AsyncSession, emp: Employee, channel: Channel) -> Actor:
    if not emp.is_active:
        raise PermissionDenied("Нет доступа: сотрудник отключён")
    dept_code = None
    if emp.department_id is not None:
        dept_code = await session.scalar(select(Department.code).where(Department.id == emp.department_id))
    rows = await session.execute(
        select(Department.code, EmployeeAccess.company_id, EmployeeAccess.can_write_kanban)
        .join(Department, Department.id == EmployeeAccess.department_id)
        .where(EmployeeAccess.employee_id == emp.id)
    )
    grants = tuple(Grant(d, c, w) for d, c, w in rows)
    return Actor(
        channel=channel,
        role=emp.role,
        employee_id=emp.id,
        name=emp.full_name,
        department=dept_code,
        company_id=emp.company_id,
        grants=grants,
    )


async def load_actor(session: AsyncSession, telegram_id: int, channel: Channel) -> Actor:
    emp = await session.scalar(select(Employee).where(Employee.telegram_id == telegram_id))
    if emp is None:
        raise PermissionDenied("Нет доступа: этот Telegram-аккаунт не зарегистрирован как сотрудник")
    return await actor_from_employee(session, emp, channel)
