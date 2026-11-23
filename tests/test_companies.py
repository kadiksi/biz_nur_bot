from sqlalchemy import delete, select

from app.agent import tools as T
from app.agent.prompts import main_system_prompt
from app.models import (Channel, ChangeLog, Company, Employee, EmployeeAccess, KanbanCard, MetricValue,
                        RecurringTask, SyncStatus)
from app.services import changes, site
from app.services.errors import PermissionDenied, ServiceError, ValidationError
from tests.conftest import OWNER_TG, SALES_HEAD
from tests.test_site import client  # noqa: F401 — фикстура

import pytest


async def _wipe_companies(session):
    for m in (KanbanCard, RecurringTask, EmployeeAccess, MetricValue, SyncStatus):
        await session.execute(delete(m))
    await session.execute(delete(Employee).where(Employee.telegram_id != OWNER_TG))
    await session.execute(delete(Company))
    await session.commit()


async def test_company_lifecycle(session, actor):
    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    with pytest.raises(ValidationError, match="латиница"):
        await changes.preview(session, owner, "companies.create", {"code": "Альфа", "name": "Альфа"})
    with pytest.raises(ValidationError, match="уже есть"):
        await changes.preview(session, owner, "companies.create", {"code": "fler", "name": "Другая"})
    with pytest.raises(ValidationError, match="есть данные"):
        await changes.preview(session, owner, "companies.delete", {"code": "fler"})

    p = await changes.apply(session, owner, "companies.create", {"code": "alpha", "name": "Альфа"})
    comp = await session.scalar(select(Company).where(Company.code == "alpha"))
    assert comp.sort_order == 3 and p.title == "Новая компания «Альфа»"
    assert "alpha — Альфа" in await main_system_prompt(session)
    await changes.apply(session, owner, "companies.update", {"code": "alpha", "name": "Альфа Трейд"})
    assert comp.name == "Альфа Трейд"
    await changes.apply(session, owner, "companies.delete", {"code": "alpha"})
    log = (await session.scalars(select(ChangeLog.action).where(ChangeLog.entity == "company"))).all()
    assert log == ["create", "update", "delete"]

    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await changes.preview(session, head, "companies.create", {"code": "beta", "name": "Бета"})
    assert "create_company" not in {t.name for t in T.tools_for(head)}


async def test_no_companies(session, actor, client):  # noqa: F811
    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    await site.save_page(session, owner, slug="summary", title="Сводка",
                         blocks=[{"type": "summary"}, {"type": "kpis", "metrics": ["revenue"]}])
    await site.validate_blocks(session, [{"type": "kpis", "metrics": ["revenue"], "company": "fler"}])
    await _wipe_companies(session)
    with pytest.raises(ValidationError, match="компаний пока нет"):
        await site.validate_blocks(session, [{"type": "kpis", "metrics": ["revenue"], "company": "fler"}])
    assert "пока ни одной" in await main_system_prompt(session)

    c = await client(OWNER_TG)
    r = await c.get("/p/summary")
    assert r.status_code == 200 and "Компании ещё не добавлены" in r.text
    assert "FLÉR" not in r.text and "Холдинг · " in r.text


async def test_chain_in_one_batch(session, actor):
    """Отдел, его показатели (в т.ч. формула) и страница — одним пакетом: каждое видит предыдущие."""
    from app.bots import pending
    from app.models import Department, MetricDefinition, SitePage
    from app.services.sandbox import Sandbox

    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    steps = [
        ("companies.create", {"code": "omega", "name": "Омега"}),
        ("departments.create", {"code": "retail", "name": "Розница"}),
        ("metrics.create", {"code": "r_rev", "name": "Выручка", "department": "retail", "unit": "₽"}),
        ("metrics.create", {"code": "r_cogs", "name": "Себестоимость", "department": "retail", "unit": "₽"}),
        ("metrics.create", {"code": "r_gp", "name": "Валовая прибыль", "department": "retail", "unit": "₽",
                            "aggregation": "derived", "formula": "r_rev - r_cogs"}),
        ("site.save_page", {"slug": "retail", "title": "Розница", "department": "retail",
                            "blocks": [{"type": "kpis", "metrics": ["r_rev", "r_gp"]}]}),
    ]
    with pytest.raises(ServiceError):  # по отдельности показатель без отдела не проходит
        await changes.preview(session, owner, *steps[2])
    async with Sandbox(session) as sb:
        previews = [await sb.step(owner, op, params) for op, params in steps]
    # в настоящей базе пока ничего нет
    assert await session.scalar(select(Department).where(Department.code == "retail")) is None
    assert await session.scalar(select(Company).where(Company.code == "omega")) is None

    res = await pending.confirm(session, owner, pending.PendingBatch("b1", 1, previews))
    assert res.changed is None and not res.errors and len(res.applied) == len(steps)
    assert await session.scalar(select(MetricDefinition).where(MetricDefinition.code == "r_gp")) is not None
    assert await session.scalar(select(SitePage).where(SitePage.slug == "retail")) is not None
    assert await session.scalar(select(Company).where(Company.code == "omega")) is not None
