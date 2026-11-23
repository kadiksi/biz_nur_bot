"""preview ничего не пишет; каждый apply = ровно одна строка change_log."""
import pytest
from sqlalchemy import func, select

from app.models import CardStatus, ChangeLog, Channel, Employee, KanbanCard, MetricValue
from app.services import changes, plans, recurring
from app.services.errors import ValidationError
from tests.conftest import OWNER_TG, SALES_FLER


async def _log_count(session):
    return await session.scalar(select(func.count()).select_from(ChangeLog))


async def _plan(session, month="2026-11"):
    from datetime import date

    from app.services import refs

    comp, m = await refs.get_company(session, "fler"), await refs.get_metric(session, "revenue")
    return await session.scalar(select(MetricValue.plan).where(
        MetricValue.company_id == comp.id, MetricValue.metric_id == m.id,
        MetricValue.period == date.fromisoformat(month + "-01")))


async def test_preview_does_not_write(session, actor):
    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    before_plan, before_log = await _plan(session), await _log_count(session)
    p = await changes.preview(session, owner, "plans.set_plan",
                              dict(company="fler", metric="revenue", month="2026-11", value=30_000_000))
    assert not p.applied and "30 000 000" in p.render().replace(" ", " ")
    session.expire_all()
    assert await _plan(session) == before_plan
    assert await _log_count(session) == before_log


async def test_apply_writes_value_and_log(session, actor):
    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    old = await _plan(session)
    p = await changes.apply(session, owner, "plans.set_plan",
                            dict(company="fler", metric="revenue", month="2026-11", value=30_000_000))
    assert p.applied and await _plan(session) == 30_000_000
    log = await session.scalar(select(ChangeLog).order_by(ChangeLog.id.desc()))
    assert (log.entity, log.action, log.channel) == ("metric_value", "set_plan", Channel.MAIN_BOT)
    assert log.before["plan"] == old and log.after["plan"] == 30_000_000
    assert log.actor_name == "Владелец" and "План" in log.summary


async def test_set_plan_for_new_month_creates_row(session, actor):
    owner = await actor(OWNER_TG)
    await plans.set_plan(session, owner, company="velse", metric="leads", month="2027-01", value=900)
    from app.services import metrics
    from app.services.periods import Period
    r = await metrics.metric(session, owner, "velse", "leads", Period.parse("2027-01"))
    assert r.plan == 900 and r.fact is None


async def _first_card(session, status=None):
    q = select(KanbanCard).order_by(KanbanCard.id)
    if status:
        q = q.where(KanbanCard.status == status)
    return await session.scalar(q)


async def _emp_id(session, tg):
    return await session.scalar(select(Employee.id).where(Employee.telegram_id == tg))


@pytest.mark.parametrize("case", [
    "set_plan", "create_card", "move_card", "update_card", "delete_card",
    "create_task", "update_task", "delete_task", "create_employee", "update_employee",
    "grant_access", "revoke_access", "sync_record",
])
async def test_every_apply_logs_exactly_once(session, actor, case):
    owner = await actor(OWNER_TG)
    card = await _first_card(session, CardStatus.BACKLOG)
    sales_fler = await _emp_id(session, SALES_FLER)
    ops = {
        "set_plan": ("plans.set_plan", dict(company="fler", metric="orders", month="2026-10", value=1)),
        "create_card": ("kanban.create_card", dict(company="fler", department="sales", title="Новая")),
        "move_card": ("kanban.move_card", dict(card_id=card.id, status="done")),
        "update_card": ("kanban.update_card", dict(card_id=card.id, title="Переименована")),
        "delete_card": ("kanban.delete_card", dict(card_id=card.id)),
        "create_task": ("recurring.create_task", dict(title="T", cron="0 9 * * *", department="sales")),
        "update_task": ("recurring.update_task", dict(task_id=1, is_active=False)),
        "delete_task": ("recurring.delete_task", dict(task_id=1)),
        "create_employee": ("employees.create", dict(telegram_id=42, full_name="Новый", department="finance")),
        "update_employee": ("employees.update", dict(employee_id=sales_fler, company=None)),
        "grant_access": ("employees.grant_access", dict(employee_id=sales_fler, department="finance")),
        "revoke_access": ("employees.revoke_access", dict(access_id=1)),
        "sync_record": ("sync.record", dict(source="1c", company="fler", department="sales", ok=True)),
    }
    op, params = ops[case]
    n = await _log_count(session)
    await changes.preview(session, owner, op, params)
    assert await _log_count(session) == n
    await changes.apply(session, owner, op, params)
    assert await _log_count(session) == n + 1


async def test_validation_errors(session, actor):
    owner = await actor(OWNER_TG)
    bad = [
        ("plans.set_plan", dict(company="fler", metric="avg_check", month="2026-11", value=1)),   # производная
        ("plans.set_plan", dict(company="holding", metric="revenue", month="2026-11", value=1)),
        ("plans.set_plan", dict(company="fler", metric="revenue", month="2026-11", value=-5)),
        ("plans.set_plan", dict(company="fler", metric="revenue", month="ноябрь", value=1)),
        ("recurring.create_task", dict(title="T", cron="каждый понедельник", department="sales")),
        ("kanban.create_card", dict(company="fler", department="sales", title="  ")),
        ("kanban.move_card", dict(card_id=1, status="archive")),
        ("employees.create", dict(telegram_id=7, full_name="X", role="owner", department=None)),
        ("plans.set_plan", dict(company="fler", metric="revenue")),  # нет параметров
        ("nope.op", {}),
    ]
    for op, params in bad:
        with pytest.raises(ValidationError):
            await changes.preview(session, owner, op, params)


async def test_owner_cannot_be_disabled_or_demoted(session, actor):
    owner = await actor(OWNER_TG)
    owner_id = await _emp_id(session, OWNER_TG)
    for params in [dict(is_active=False), dict(role="head")]:
        with pytest.raises(ValidationError):
            await changes.preview(session, owner, "employees.update", dict(employee_id=owner_id, **params))


async def test_recurring_next_run_in_local_time(session, actor):
    from datetime import datetime
    sat = datetime(2026, 11, 21, 9, 0)  # суббота, UTC
    assert recurring.next_run("0 10 * * 1", now=sat) == datetime(2026, 11, 23, 7, 0)  # пн 10:00 МСК
    assert recurring.next_run("0 10 * * 0", now=sat) == datetime(2026, 11, 22, 7, 0)  # вс
    assert recurring.next_run("0 10 * * 7", now=sat) == datetime(2026, 11, 22, 7, 0)  # вс (7)
    assert recurring.next_run("0 10 * * 1-5", now=sat) == datetime(2026, 11, 23, 7, 0)
    assert recurring.next_run("0 10 * * mon-fri", now=sat) == datetime(2026, 11, 23, 7, 0)
    assert recurring.next_run("30 9 1 * *", now=sat) == datetime(2026, 12, 1, 6, 30)
    for bad in ["0 10 * * 8", "0 25 * * *", "0 10 * *"]:
        with pytest.raises(ValidationError):
            recurring.next_run(bad)
