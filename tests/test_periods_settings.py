from datetime import date

import pytest
from sqlalchemy import select

from app.agent import tools as T
from app.integrations import onec as api
from app.models import Channel, ChangeLog, MetricDefinition
from app.services import changes, metrics, onec, sitecfg
from app.services.errors import PermissionDenied, ValidationError
from app.services.periods import Period
from tests import fake_onec as F
from tests.conftest import OWNER_TG, SALES_HEAD
from tests.test_onec import ENV, ORDERS, REVENUE
from tests.test_site import client  # noqa: F401 — фикстура


@pytest.fixture
def fake(monkeypatch):
    srv = F.FakeOneC()
    monkeypatch.setattr(api, "TRANSPORT", srv.transport())
    monkeypatch.setattr(api, "_env", lambda: dict(ENV))
    onec._live_cache.clear()
    return srv


@pytest.fixture
async def owner(actor):
    return await actor(OWNER_TG, Channel.MAIN_BOT)


@pytest.mark.parametrize("spec, label, partial", [
    ("2026-05-03..2026-11-20", "3 мая – 20 ноября 2026", True),
    ("2026-10-01..2026-10-31", "октябрь 2026", False),
    ("2026-10-05..2026-10-20", "5 – 20 октября 2026", True),
    ("2025-12-15..2026-01-10", "15 декабря 2025 – 10 января 2026", True),
    ("2026-11-21", "21 ноября 2026", True),
])
def test_day_periods(spec, label, partial):
    p = Period.parse(spec)
    assert p.label == label and p.is_partial == partial
    assert Period.parse(p.spec) == p


def test_day_period_errors():
    with pytest.raises(ValidationError):
        Period.parse("2026-11-20..2026-11-01")
    with pytest.raises(ValidationError):
        Period.parse("2026-02-30..2026-03-01")


async def test_partial_period_uses_1c_and_prorates_plan(session, owner, fake):
    await changes.apply(session, owner, "onec.save_source",
                        dict(metric="revenue", company="fler", connection="fler", **REVENUE))
    full = await metrics.metric(session, owner, "fler", "revenue", Period.parse("2026-10"))
    # 1–15 октября: в 1С у ООО «Флер» за эти дни 1 000 000 (движение 20.10 не входит)
    p = Period.parse("2026-10-01..2026-10-15")
    r = await metrics.metric(session, owner, "fler", "revenue", p)
    assert r.fact == 1_000_000
    assert r.plan == pytest.approx(full.plan * 15 / 31)
    # заказы не подключены к 1С — факт за неполный месяц неизвестен, а не «часть месяца»
    o = await metrics.metric(session, owner, "fler", "orders", p)
    assert o.fact is None and o.plan == pytest.approx(
        (await metrics.metric(session, owner, "fler", "orders", Period.parse("2026-10"))).plan * 15 / 31)
    # производный показатель без заказов тоже неизвестен
    assert (await metrics.metric(session, owner, "fler", "avg_check", p)).fact is None
    # период из целого октября и части ноября: октябрь — сохранённый, ноябрь — из 1С
    p2 = Period.parse("2026-10-01..2026-11-10")
    r2 = await metrics.metric(session, owner, "fler", "revenue", p2)
    assert r2.series[0].fact == full.fact and r2.series[1].fact == 700_000
    n_requests = len(fake.requests)
    await metrics.metric(session, owner, "fler", "revenue", p2)
    assert len(fake.requests) == n_requests  # второй раз — из кеша


async def test_site_settings(session, owner, actor, client):  # noqa: F811
    with pytest.raises(ValidationError, match="слишком светлый"):
        await changes.preview(session, owner, "site.update_settings", {"brand_color": "#f0f0f0"})
    with pytest.raises(ValidationError, match="#RRGGBB"):
        await changes.preview(session, owner, "site.update_settings", {"brand_color": "red"})
    p = await changes.apply(session, owner, "site.update_settings",
                            {"brand_name": "Remely Group", "brand_color": "#1d3b2a", "default_period": "prev_month"})
    assert "Corner Group → Remely Group" in p.render()
    cfg = await sitecfg.get_all(session)
    assert cfg["brand_name"] == "Remely Group" and cfg["default_period"] == "prev_month"
    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await changes.preview(session, head, "site.update_settings", {"brand_name": "X"})

    from app.services import site
    await site.save_page(session, owner, slug="summary", title="Сводка", blocks=[{"type": "summary"}])
    c = await client(OWNER_TG)
    r = await c.get("/p/summary")
    assert "Remely Group" in r.text and "--brand: #1d3b2a" in r.text and "октябрь 2026" in r.text
    r = await c.get("/p/summary?period=custom&from=2026-10-05&to=2026-10-20")
    assert "5 – 20 октября 2026" in r.text and "Свои даты" in r.text and 'value="2026-10-05"' in r.text
    assert "Период с точными датами" in r.text
    r = await c.get("/p/summary?period=month&from=2026-10-05&to=2026-10-20")  # выбран готовый период — даты не мешают
    assert "ноябрь 2026" in r.text
    await changes.apply(session, owner, "site.update_settings", {"brand_name": None})
    assert (await sitecfg.get_all(session))["brand_name"] == "Corner Group"


async def test_structure(session, owner):
    with pytest.raises(ValidationError, match="есть показатели"):
        await changes.preview(session, owner, "departments.delete", {"code": "sales"})
    await changes.apply(session, owner, "departments.create", {"code": "logistics", "name": "Логистика"})
    await changes.apply(session, owner, "metrics.create",
                        {"code": "deliveries", "name": "Доставки", "department": "logistics", "unit": "шт"})
    await changes.apply(session, owner, "metrics.create",
                        {"code": "rev_per_delivery", "name": "Выручка на доставку", "department": "logistics",
                         "unit": "₽", "aggregation": "derived", "formula": "revenue / deliveries"})
    with pytest.raises(ValidationError, match="сами считаются формулой"):
        await changes.preview(session, owner, "metrics.create",
                              {"code": "x2", "name": "X", "department": "logistics", "unit": "₽",
                               "aggregation": "derived", "formula": "avg_check * 2"})
    with pytest.raises(ValidationError, match="неизвестные"):
        await changes.preview(session, owner, "metrics.create",
                              {"code": "x3", "name": "X", "department": "logistics", "unit": "₽",
                               "aggregation": "derived", "formula": "revenue / nope"})
    with pytest.raises(ValidationError, match="формулы: rev_per_delivery"):
        await changes.preview(session, owner, "metrics.delete", {"code": "deliveries"})
    with pytest.raises(ValidationError, match="планы и факты"):
        await changes.preview(session, owner, "metrics.update", {"code": "revenue", "aggregation": "derived",
                                                                 "formula": "orders * 2"})
    p = await changes.apply(session, owner, "metrics.update", {"code": "deliveries", "name": "Доставки, шт",
                                                               "is_key": True})
    assert "Доставки → Доставки, шт" in p.render()
    rows = await metrics.department_metrics(session, owner, "fler", "logistics", Period.parse("2026-10"))
    assert [r.code for r in rows] == ["deliveries", "rev_per_delivery"]
    await changes.apply(session, owner, "metrics.delete", {"code": "rev_per_delivery"})
    await changes.apply(session, owner, "metrics.delete", {"code": "deliveries"})
    await changes.apply(session, owner, "departments.delete", {"code": "logistics"})
    log = (await session.scalars(select(ChangeLog.entity).where(ChangeLog.entity.in_(
        ["department", "metric_definition"])))).all()
    assert len(log) == 7
    assert await session.scalar(select(MetricDefinition).where(MetricDefinition.code == "deliveries")) is None


async def test_tools_listed(owner, actor):
    names = {t.name for t in T.tools_for(owner)}
    assert {"update_site_settings", "create_metric", "update_department", "get_site_settings"} <= names
    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    assert not {"update_site_settings", "create_metric"} & {t.name for t in T.tools_for(head)}
