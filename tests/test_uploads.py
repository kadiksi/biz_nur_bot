import io
import json
from datetime import date, datetime

import openpyxl
import pytest
from sqlalchemy import select

from app.agent import tools as T
from app.agent import uploads
from app.models import Channel, Company, MetricDefinition, MetricValue, OneCSource
from app.services import changes
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import OWNER_TG, SALES_HEAD


def _xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Расходы"
    ws.append(["Статья", datetime(2026, 9, 1), datetime(2026, 10, 1)])
    ws.append(["Аренда", 500000, 500000.0])
    ws.append(["Зарплата", 1200000.5, 1300000])
    ws.append(["Итого", "=B2+B3", "=C2+C3"])
    for i in range(100):
        ws.append([f"строка {i}", i])
    wb.create_sheet("Пусто")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_overview_and_read():
    up, blocks = uploads.add("Расходы 2026.xlsx", _xlsx())
    text = blocks[0]["text"]
    assert f"file_id={up.id}" in text and "Лист «Расходы»: 104 строк × 3 колонок" in text
    assert "1: Статья | 2026-09-01 | 2026-10-01" in text and "2: Аренда | 500000 | 500000" in text
    assert "3: Зарплата | 1200000.5 | 1300000" in text and "ещё 44 строк" in text and "Лист «Пусто»: 0 строк" in text
    part = uploads.read(up.id, "Расходы", start_row=100, rows=10)
    assert part["total_rows"] == 104 and part["rows"].startswith("100: строка 95 | 95")
    with pytest.raises(ValidationError, match="Листа"):
        uploads.read(up.id, "Нет такого")


def test_csv_cp1251_and_formats():
    data = "Статья;Октябрь\nАренда;1 234,5\nРеклама;300\n".encode("cp1251")
    up, blocks = uploads.add("r.csv", data)
    assert up.sheets["CSV"][1] == ["Аренда", 1234.5] and up.sheets["CSV"][2] == ["Реклама", 300]
    _, img = uploads.add("p.PNG", b"\x89PNG....")
    assert img[0]["type"] == "image" and img[0]["source"]["media_type"] == "image/png"
    _, pdf = uploads.add("a.pdf", b"%PDF-1.4")
    assert pdf[0]["type"] == "document"
    for name, msg in (("old.xls", "xlsx"), ("a.exe", "не читаю"), ("bad.xlsx", "Excel")):
        with pytest.raises(ValidationError, match=msg):
            uploads.add(name, b"junk")
    with pytest.raises(Exception, match="ещё раз"):
        uploads.get("nope")


async def test_set_facts(session, actor):
    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    await changes.apply(session, owner, "departments.create", {"code": "costs", "name": "Расходы"})
    for code in ("rent", "salary"):
        await changes.apply(session, owner, "metrics.create", {"code": code, "name": code, "department": "costs",
                                                                "unit": "₽", "higher_is_better": False})
    await changes.apply(session, owner, "metrics.create", {"code": "costs_total", "name": "Расходы всего",
                                                            "department": "costs", "unit": "₽",
                                                            "aggregation": "derived", "formula": "rent + salary"})
    args = {"company": "fler", "metric": "rent", "values": {"2026-09": 500000, "2026-10": "500000"},
            "source_note": "Расходы 2026.xlsx"}
    p = await changes.preview(session, owner, "facts.set_facts", args)
    assert "2 мес." in p.title and "Расходы 2026.xlsx" in p.title
    await changes.apply(session, owner, "facts.set_facts", args)
    comp = await session.scalar(select(Company).where(Company.code == "fler"))
    rent = await session.scalar(select(MetricDefinition).where(MetricDefinition.code == "rent"))
    rows = (await session.scalars(select(MetricValue).where(MetricValue.metric_id == rent.id))).all()
    assert {(r.period, r.fact, r.source) for r in rows} == {(date(2026, 9, 1), 500000, "file"),
                                                            (date(2026, 10, 1), 500000, "file")}
    from app.services import sync

    fresh = await sync.freshness(session, owner, "fler", "costs")
    assert fresh.label.startswith("из файла · загружено") and not fresh.is_stale
    # повторно — видно «было → стало»
    p = await changes.preview(session, owner, "facts.set_facts", {**args, "values": {"2026-10": 510000}})
    assert p.changes[0].before is not None

    with pytest.raises(ValidationError, match="формуле"):
        await changes.preview(session, owner, "facts.set_facts", {**args, "metric": "costs_total"})
    with pytest.raises(ValidationError, match="числом"):
        await changes.preview(session, owner, "facts.set_facts", {**args, "values": {"2026-10": "много"}})
    # из 1С — не трогаем
    session.add(MetricValue(company_id=comp.id, metric_id=rent.id, period=date(2026, 8, 1), fact=1, source="1c"))
    await session.commit()
    with pytest.raises(ValidationError, match="из 1С"):
        await changes.preview(session, owner, "facts.set_facts", {**args, "values": {"2026-08": 2}})
    session.add(OneCSource(metric_id=rent.id, company_id=comp.id, connection="x", entity="Document_X"))
    await session.commit()
    with pytest.raises(ValidationError, match="берётся из 1С"):
        await changes.preview(session, owner, "facts.set_facts", args)
    with pytest.raises(PermissionDenied):
        await changes.preview(session, await actor(SALES_HEAD, Channel.DEPT_BOT), "facts.set_facts", args)


async def test_tools(session, actor):
    owner = await actor(OWNER_TG, Channel.MAIN_BOT)
    allowed = {t.name: t for t in T.tools_for(owner)}
    up, _ = uploads.add("x.xlsx", _xlsx())
    out = await T.execute(session, owner, "read_file", {"file_id": up.id, "start_row": 2, "rows": 2}, allowed)
    assert json.loads(out.content)["rows"] == "2: Аренда | 500000 | 500000\n3: Зарплата | 1200000.5 | 1300000"
    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    assert not {"read_file", "set_facts"} & {t.name for t in T.tools_for(head)}
