import pytest

from app.models import Channel
from app.services import employees, kanban, metrics, sync
from app.services.changes import apply, preview
from app.services.errors import PermissionDenied
from app.services.periods import Period
from tests.conftest import INACTIVE, MARKETING_REMELY, OWNER_TG, SALES_FLER, SALES_HEAD, TODAY

P = Period.parse("half", TODAY)
DEPTS = ["sales", "marketing", "warehouse", "finance"]


async def test_owner_reads_everything(session, actor):
    owner = await actor(OWNER_TG)
    for company in ["holding", "fler", "remely", "velse"]:
        assert await metrics.summary_metrics(session, owner, company, P)
        for d in DEPTS:
            assert await metrics.department_metrics(session, owner, company, d, P)
            await kanban.board(session, owner, company, d)
            await sync.freshness(session, owner, company, d)


async def test_employee_sees_only_own_department_and_company(session, actor):
    emp = await actor(SALES_FLER)
    assert await metrics.department_metrics(session, emp, "fler", "sales", P)
    for company, dept in [("remely", "sales"), ("holding", "sales"), ("fler", "marketing"), ("fler", "finance")]:
        with pytest.raises(PermissionDenied):
            await metrics.department_metrics(session, emp, company, dept, P)
    with pytest.raises(PermissionDenied):
        await kanban.board(session, emp, "fler", "warehouse")


async def test_summary_visible_within_own_companies(session, actor):
    emp = await actor(SALES_FLER)
    assert await metrics.summary_metrics(session, emp, "fler", P)
    for company in ["remely", "holding"]:
        with pytest.raises(PermissionDenied):
            await metrics.summary_metrics(session, emp, company, P)


async def test_key_metric_of_other_department_via_summary_only(session, actor):
    emp = await actor(SALES_FLER)
    assert (await metrics.metric(session, emp, "fler", "net_profit", P)).fact is not None  # ключевой
    with pytest.raises(PermissionDenied):
        await metrics.metric(session, emp, "fler", "opex", P)  # не ключевой, чужой отдел


async def test_head_without_company_sees_holding_of_own_department(session, actor):
    head = await actor(SALES_HEAD)
    assert await metrics.department_metrics(session, head, "holding", "sales", P)
    assert await metrics.summary_metrics(session, head, "holding", P)
    with pytest.raises(PermissionDenied):
        await metrics.department_metrics(session, head, "fler", "marketing", P)


async def test_grant_gives_read_but_not_kanban_write(session, actor):
    emp = await actor(MARKETING_REMELY)
    assert await metrics.department_metrics(session, emp, "fler", "sales", P)
    await kanban.board(session, emp, "fler", "sales")
    with pytest.raises(PermissionDenied):
        await kanban.create_card(session, emp, company="fler", department="sales", title="x")


async def test_inactive_and_unknown_are_denied(session, actor):
    with pytest.raises(PermissionDenied):
        await actor(INACTIVE)
    with pytest.raises(PermissionDenied):
        await actor(123)


@pytest.mark.parametrize("tg", [SALES_HEAD, SALES_FLER])
@pytest.mark.parametrize(
    "op,params",
    [
        ("plans.set_plan", dict(company="fler", metric="revenue", month="2026-12", value=1)),
        ("recurring.create_task", dict(title="x", cron="0 10 * * 1", department="sales")),
        ("recurring.delete_task", dict(task_id=1)),
        ("employees.create", dict(telegram_id=1, full_name="x", department="sales")),
        ("employees.update", dict(employee_id=2, is_active=False)),
        ("employees.grant_access", dict(employee_id=2, department="finance")),
        ("sync.record", dict(source="1c", company="fler", department="sales", ok=True)),
    ],
)
async def test_owner_only_operations(session, actor, tg, op, params):
    a = await actor(tg, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await preview(session, a, op, params)
    with pytest.raises(PermissionDenied):
        await apply(session, a, op, params)


async def test_employee_lists_only_department_colleagues(session, actor):
    emp = await actor(SALES_FLER)
    names = {e.full_name for e in await employees.list_employees(session, emp)}
    assert "Анна Соколова" in names and "Илья Петров" in names
    assert "Мария Кузнецова" not in names  # маркетинг
    assert "Кирилл Зайцев" not in names    # отключён
