"""Поиск справочных сущностей по коду."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, Department, Employee, MetricDefinition
from app.services.errors import NotFound, ValidationError

HOLDING = "holding"
HOLDING_NAME = "Холдинг"


async def list_companies(session: AsyncSession) -> list[Company]:
    return list(await session.scalars(select(Company).order_by(Company.sort_order)))


async def list_departments(session: AsyncSession) -> list[Department]:
    return list(await session.scalars(select(Department).order_by(Department.sort_order)))


async def get_company(session: AsyncSession, code: str) -> Company:
    if code == HOLDING:
        raise ValidationError("Здесь нужна конкретная компания, а не холдинг")
    comp = await session.scalar(select(Company).where(Company.code == code))
    if comp is None:
        raise NotFound(f"Компания «{code}» не найдена")
    return comp


async def resolve_companies(session: AsyncSession, code: str) -> list[Company]:
    """'holding' → все компании, иначе одна."""
    if code == HOLDING:
        return await list_companies(session)
    return [await get_company(session, code)]


async def get_department(session: AsyncSession, code: str) -> Department:
    dept = await session.scalar(select(Department).where(Department.code == code))
    if dept is None:
        raise NotFound(f"Отдел «{code}» не найден")
    return dept


async def get_metric(session: AsyncSession, code: str) -> MetricDefinition:
    m = await session.scalar(select(MetricDefinition).where(MetricDefinition.code == code))
    if m is None:
        raise NotFound(f"Показатель «{code}» не найден")
    return m


async def get_employee(session: AsyncSession, employee_id: int) -> Employee:
    emp = await session.get(Employee, employee_id)
    if emp is None:
        raise NotFound(f"Сотрудник #{employee_id} не найден")
    return emp
