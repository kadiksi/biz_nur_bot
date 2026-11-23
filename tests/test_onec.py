from datetime import date

import pytest
from sqlalchemy import select

from app.agent import tools as T
from app.integrations import onec as api
from app.models import Channel, ChangeLog, MetricValue, SyncState, SyncStatus
from app.services import changes, onec, sync
from app.services.context import Actor
from app.services.errors import PermissionDenied, ValidationError
from tests import fake_onec as F
from tests.conftest import OWNER_TG, SALES_HEAD
from tests.test_site import client  # noqa: F401 — фикстура

ENV = {"ONEC_FLER_URL": F.BASE + "/", "ONEC_FLER_USER": F.USER, "ONEC_FLER_PASSWORD": F.PASSWORD,
       "ONEC_FLER_COMPANY": "fler", "ONEC_BAD_URL": F.BASE, "ONEC_BAD_USER": "x", "ONEC_BAD_PASSWORD": "y",
       "ONEC_SYNC_MINUTES": "60", "BOT_TOKEN_MAIN": "z"}

REVENUE = dict(entity="AccumulationRegister_ВыручкаИСебестоимостьПродаж/Turnovers",
               field="СуммаВыручкиБезНДСTurnover", period_mode="turnovers",
               filter=f"Организация_Key eq guid'{F.ORG_FLER}'")
ORDERS = dict(entity="Document_ЗаказКлиента", aggregation="count", period_mode="date_field", date_field="Date",
              filter="Posted eq true")
CASH = dict(entity="AccumulationRegister_ДенежныеСредства/Balance", field="СуммаBalance", period_mode="balance")


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    srv = F.FakeOneC()
    monkeypatch.setattr(api, "TRANSPORT", srv.transport())
    monkeypatch.setattr(api, "_env", lambda: dict(ENV))
    monkeypatch.setattr(api, "today_local", lambda: date(2026, 11, 21), raising=False)
    monkeypatch.setattr(onec, "today_local", lambda: date(2026, 11, 21))
    api.Client._metadata_cache.clear()
    return srv


@pytest.fixture
async def owner(actor):
    return await actor(OWNER_TG, Channel.MAIN_BOT)


def test_connections_from_env():
    conns = api.load_connections(ENV)
    assert list(conns) == ["bad", "fler"]
    c = conns["fler"]
    assert c.url == F.BASE and c.user == F.USER and c.company == "fler" and c.verify_ssl
    assert "secret" not in repr(c)
    with pytest.raises(ValidationError, match="onec_request_connection"):
        api.get_connection("nope")


def test_build_request():
    src = api.Source(**REVENUE).validate()
    entity, flt = api.build_request(src, date(2026, 2, 1))
    assert entity.endswith("/Turnovers(StartPeriod=datetime'2026-02-01T00:00:00',EndPeriod=datetime'2026-02-28T23:59:59')")
    bal, _ = api.build_request(api.Source(**CASH).validate(), date(2026, 12, 1))
    assert bal.endswith("(Period=datetime'2027-01-01T00:00:00')")
    _, f2 = api.build_request(api.Source(**ORDERS).validate(), date(2026, 10, 1))
    assert f2 == "(Posted eq true) and Date ge datetime'2026-10-01T00:00:00' and Date le datetime'2026-10-31T23:59:59'"


@pytest.mark.parametrize("bad, msg", [
    (dict(entity="Document_X?$x=1", period_mode="none", aggregation="count"), "Недопустимое имя"),
    (dict(entity="Document_X", period_mode="none"), "нужно поле"),
    (dict(entity="Document_X", field="Сумма", period_mode="turnovers"), "виртуальная таблица"),
    (dict(entity="AccumulationRegister_X/Turnovers", field="Сумма", period_mode="balance"), "остатка"),
    (dict(entity="Document_X", aggregation="count", period_mode="date_field"), "поле даты"),
    (dict(entity="Document_X", aggregation="count", period_mode="none", filter="a eq 1&$top=1"), "Недопустимые"),
])
def test_source_validation(bad, msg):
    with pytest.raises(ValidationError, match=msg):
        api.Source(**bad).validate()


async def test_browse(owner, fake):
    conns = await onec.connections(owner)
    assert {c["name"]: c["ok"] for c in conns} == {"bad": False, "fler": True}
    assert "не пустила" in conns[0]["error"] and "secret" not in str(conns)
    found = await onec.find_objects(owner, "fler", search="выручк")
    assert found["objects"] == ["AccumulationRegister_ВыручкаИСебестоимостьПродаж"]
    assert (await onec.find_objects(owner, "fler", kind="Catalog"))["found"] == 2

    d = await onec.describe(owner, "fler", "AccumulationRegister_ВыручкаИСебестоимостьПродаж")
    assert d["virtual_tables"] == ["AccumulationRegister_ВыручкаИСебестоимостьПродаж/Turnovers"]
    d = await onec.describe(owner, "fler", "AccumulationRegister_ВыручкаИСебестоимостьПродаж/Turnovers")
    assert [f["name"] for f in d["fields"]] == ["Организация_Key", "СуммаВыручкиБезНДСTurnover"]

    rows = await onec.query(owner, "fler", "Catalog_Организации", top=5)
    assert rows["rows"][0]["Description"] == "ООО «Флер»"
    with pytest.raises(api.OneCError, match="не включён в состав OData"):
        await onec.query(owner, "fler", "Document_Нет")
    assert all(r.method == "GET" for r in fake.requests)


async def test_compute_modes(owner):
    r = await onec.test_source(owner, "fler", month="2026-10", **REVENUE)
    assert r["value"] == 1_500_000 and r["rows_used"] == 1           # чужая организация отфильтрована
    assert (await onec.test_source(owner, "fler", month="2026-10", **ORDERS))["value"] == 2  # непроведённый не считаем
    assert (await onec.test_source(owner, "fler", month="2026-10", **CASH))["value"] == 1_500_000
    neg = await onec.test_source(owner, "fler", month="2026-11", **(CASH | {"multiplier": 0.001}))
    assert neg["value"] == 1600
    with pytest.raises(api.OneCError, match="ошибку 400: Неправильное имя поля: Сумма"):
        await onec.test_source(owner, "fler", month="2026-10", **(REVENUE | {"field": "Сумма"}))


async def test_owner_only(session, actor):
    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await onec.connections(head)
    with pytest.raises(PermissionDenied):
        await changes.preview(session, head, "onec.load_facts", {})
    assert not any(t.name.startswith("onec_") for t in T.tools_for(head))


async def test_save_and_load(session, owner):
    with pytest.raises(ValidationError, match="формулой"):
        await changes.preview(session, owner, "onec.save_source",
                              dict(metric="avg_check", company="fler", connection="fler", **REVENUE))
    with pytest.raises(ValidationError, match="Нет настроенных"):
        await changes.preview(session, owner, "onec.load_facts", {})

    for metric, src in (("revenue", REVENUE), ("orders", ORDERS), ("cash", CASH)):
        p = await changes.preview(session, owner, "onec.save_source",
                                  dict(metric=metric, company="fler", connection="FLER", **src))
        assert p.changes and p.title.startswith("Источник в 1С")
        await changes.apply(session, owner, "onec.save_source", p.params)
    with pytest.raises(ValidationError, match="менять нечего"):
        await changes.preview(session, owner, "onec.save_source",
                              dict(metric="revenue", company="fler", connection="fler", **REVENUE))
    upd = await changes.preview(session, owner, "onec.save_source",
                                dict(metric="revenue", company="fler", connection="fler", **REVENUE, multiplier=1.0,
                                     note="без НДС"))
    assert [c.label for c in upd.changes] == ["Заметка"]
    assert len(await onec.list_sources(session, owner)) == 3

    p = await changes.preview(session, owner, "onec.load_facts", {"period": "2026-10..2026-11"})
    assert "Выручка (FLÉR)" in p.render()
    res = await changes.apply(session, owner, "onec.load_facts", p.params)
    assert "источников 3 из 3" in res.title and "изменилось значений: 6" in res.title

    vals = {(v.metric_id, v.period): v for v in await session.scalars(
        select(MetricValue).where(MetricValue.source == "1c"))}
    assert len(vals) == 6
    facts = sorted(v.fact for v in vals.values())
    assert facts == [1.0, 2.0, 1_000_000, 1_500_000, 1_500_000, 1_600_000]
    assert all(v.plan is not None for v in vals.values())  # планы не тронуты

    log = (await session.scalars(select(ChangeLog).where(ChangeLog.action == "load_1c"))).all()
    assert len(log) == 1 and "Выручка · FLÉR · октябрь 2026" in log[0].summary

    # повторная загрузка: ничего не изменилось — журнал не пополняется
    again = await changes.apply(session, owner, "onec.load_facts", p.params)
    assert "изменилось значений: 0" in again.title
    assert len((await session.scalars(select(ChangeLog).where(ChangeLog.action == "load_1c"))).all()) == 1

    statuses = (await session.scalars(select(SyncStatus).where(SyncStatus.company_id == 1))).all()
    by = {(s.source, s.department_id) for s in statuses}
    assert ("demo", 1) not in by and ("1c", 1) in by  # ОП FLÉR теперь из 1С
    fr = await sync.freshness(session, owner, "fler", "sales")
    assert fr.label == "обновлено только что" and not fr.has_error


async def test_load_errors(session, owner, monkeypatch):
    await changes.apply(session, owner, "onec.save_source",
                        dict(metric="revenue", company="fler", connection="fler", **REVENUE))
    await changes.apply(session, owner, "onec.save_source",
                        dict(metric="orders", company="fler", connection="bad", **ORDERS))
    res = await onec.load_facts(session, Actor.system(Channel.SCHEDULER))
    assert "источников 1 из 2" in res.title and "ошибок: 1" in res.title
    st = await session.scalar(select(SyncStatus).where(SyncStatus.source == "1c", SyncStatus.department_id == 1))
    assert st.status == SyncState.ERROR and "не пустила" in st.message
    fr = await sync.freshness(session, owner, "fler", "sales")
    assert fr.has_error
    srcs = {s["metric"]: s for s in await onec.list_sources(session, owner)}
    assert srcs["orders"]["last_error"] and srcs["revenue"]["last_error"] is None


async def test_scheduled_load(session):
    sysactor = Actor.system(Channel.SCHEDULER)
    assert await onec.scheduled_load(session, sysactor) is None  # нечего загружать — тихо


async def test_tools(session, owner):
    allowed = {t.name: t for t in T.tools_for(owner)}
    out = await T.execute(session, owner, "onec_find_objects", {"connection": "fler", "search": "заказ"}, allowed)
    assert "Document_ЗаказКлиента" in out.content and not out.is_error
    out = await T.execute(session, owner, "onec_test_source",
                          {"connection": "fler", "month": "2026-10", **REVENUE}, allowed)
    assert '"value": 1500000' in out.content
    out = await T.execute(session, owner, "onec_query", {"connection": "fler", "entity": "Document_Нет"}, allowed)
    assert out.is_error and "OData" in out.content
    out = await T.execute(session, owner, "onec_save_source",
                          {"metric": "revenue", "company": "fler", "connection": "fler", **REVENUE}, allowed)
    assert out.preview and "НЕ применено" in out.content
    for t in T.WRITE_TOOLS + T.READ_TOOLS:
        assert t.schema["type"] == "object"


async def test_new_source_loaded_with_history(session, owner):
    sysactor = Actor.system(Channel.SCHEDULER)
    await changes.apply(session, owner, "onec.save_source",
                        dict(metric="revenue", company="fler", connection="fler", **REVENUE))
    assert (await onec.scheduled_load(session, sysactor, due=False)).startswith("Загрузка из 1С · июнь–ноябрь 2026")
    assert await onec.scheduled_load(session, sysactor, due=False) is None   # уже загружен, до срока — ждём
    assert (await onec.scheduled_load(session, sysactor, due=True)).startswith("Загрузка из 1С · октябрь–ноябрь 2026")


async def test_progress_tracking(session, owner, fake, monkeypatch):
    for metric, src in (("revenue", REVENUE), ("orders", ORDERS)):
        await changes.apply(session, owner, "onec.save_source",
                            dict(metric=metric, company="fler", connection="fler", **src))
    assert onec.wake_event().is_set()  # новый источник будит фоновую загрузку
    seen = []
    real = api.compute

    async def spy(cl, src, month):
        p = onec.progress()
        seen.append((p["running"], p["done"], p["total"], p["current"]))
        return await real(cl, src, month)

    monkeypatch.setattr(api, "compute", spy)
    run = onec.PROGRESS.run_id
    await onec.load_facts(session, owner, period="2026-10..2026-11")
    assert seen[0] == (True, 0, 4, "Выручка · FLÉR · октябрь 2026") and seen[-1][1] == 3
    p = onec.progress()
    assert p["run_id"] == run + 1 and not p["running"] and p["percent"] == 100 and p["changed"] == 4
    assert "источников 2 из 2" in p["result"]


async def test_progress_api(session, actor, client):
    from tests.conftest import SALES_FLER
    c = await client(OWNER_TG)
    p = (await c.get("/api/sync/progress")).json()
    assert {"running", "percent", "current", "result"} <= set(p)
    emp = await client(SALES_FLER)
    p = (await emp.get("/api/sync/progress")).json()
    assert "current" not in p and "result" not in p
    anon = await client()
    assert (await anon.get("/api/sync/progress")).status_code == 401
