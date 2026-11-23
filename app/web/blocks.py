"""Данные для блоков страницы. Всё читается через app/services с правами зрителя."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, Employee
from app.services import kanban, metrics, refs
from app.services.access import can_read_summary, can_write_kanban
from app.services.context import Actor
from app.services.errors import ServiceError
from app.services.format import NBSP, fmt_date, fmt_number
from app.services.kanban import PRIORITY_LABELS, STATUS_LABELS
from app.services.periods import Period, add_months, today_local

PERIODS = [
    ("month", "Текущий месяц"),
    ("prev_month", "Прошлый месяц"),
    ("quarter", "Квартал"),
    ("half", "Полгода"),
    ("ytd", "С начала года"),
]

MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


# ---------- форматирование ----------


def fmt_compact(value: float | None, unit: str, decimals: int = 0) -> str:
    """24 300 000 ₽ → «24,3 млн ₽»; мелкие значения — как есть."""
    if value is None:
        return "—"
    a = abs(value)
    if unit == "₽" and a >= 1_000_000:
        return f"{fmt_number(value / 1_000_000, 1)}{NBSP}млн{NBSP}₽"
    if unit == "₽" and a >= 10_000:
        return f"{fmt_number(value / 1_000, 0)}{NBSP}тыс{NBSP}₽"
    return f"{fmt_number(value, decimals)}{NBSP}{unit}".rstrip()


def status_of(pct: float | None) -> str:
    if pct is None:
        return "none"
    if pct >= 100:
        return "good"
    if pct >= 90:
        return "warn"
    return "bad"


def row_view(r: metrics.MetricRow) -> dict[str, Any]:
    dev = r.deviation
    return {
        "code": r.code, "name": r.name, "unit": r.unit,
        "fact": fmt_compact(r.fact, r.unit, r.decimals),
        "plan": fmt_compact(r.plan, r.unit, r.decimals),
        "dev": ("+" if dev and dev > 0 else "") + fmt_compact(dev, r.unit, r.decimals) if dev is not None else "—",
        "pct": None if r.completion is None else round(r.completion),
        "bar": None if r.completion is None else max(0, min(100, round(r.completion))),
        "status": status_of(r.completion),
        "lower_better": not r.higher_is_better,
    }


# ---------- контекст страницы ----------


@dataclass
class ViewContext:
    actor: Actor
    company: str          # код или holding
    period: Period
    period_key: str
    companies: list[Company] = field(default_factory=list)  # видимые зрителю


async def visible_companies(session: AsyncSession, actor: Actor) -> list[Company]:
    return [c for c in await refs.list_companies(session) if can_read_summary(actor, c.id)]


def company_options(companies: list[Company], all_companies: int,
                    holding_label: str = "Холдинг") -> list[tuple[str, str]]:
    opts = [(c.code, c.name) for c in companies]
    if companies and len(companies) == all_companies:
        opts.insert(0, ("holding", holding_label))
    return opts


def _chart_period(p: Period) -> Period:
    """Для графика одного месяца мало — показываем полгода до него."""
    if p.start == p.end:
        return Period(add_months(p.end, -5), p.end)
    return p


def _month_labels(months: list[date]) -> list[str]:
    multi_year = len({m.year for m in months}) > 1
    return [f"{MONTHS_SHORT[m.month - 1]}{' ' + str(m.year)[2:] if multi_year else ''}" for m in months]


# ---------- блоки ----------


async def _kpis(session, ctx, company, b):
    items = []
    for code in b["metrics"]:
        items.append(row_view(await metrics.metric(session, ctx.actor, company, code, ctx.period)))
    return {"items": items}


async def _table(session, ctx, company, b):
    if b.get("department"):
        rows = await metrics.department_metrics(session, ctx.actor, company, b["department"], ctx.period)
        dept = await refs.get_department(session, b["department"])
        return {"rows": [row_view(r) for r in rows], "default_title": f"{dept.name}: план и факт"}
    rows = [await metrics.metric(session, ctx.actor, company, c, ctx.period) for c in b["metrics"]]
    return {"rows": [row_view(r) for r in rows], "default_title": "План и факт"}


async def _chart(session, ctx, company, b):
    p = _chart_period(ctx.period)
    labels = _month_labels(p.months())
    if b.get("compare_companies") and company == "holding":
        datasets = []
        name = unit = None
        for c in ctx.companies:
            r = await metrics.metric(session, ctx.actor, c.code, b["metric"], p)
            name, unit = r.name, r.unit
            datasets.append({"label": c.name, "data": [pt.fact for pt in r.series]})
        return {"chart": {"kind": b.get("kind", "bar"), "labels": labels, "datasets": datasets,
                          "unit": unit, "compare": True},
                "default_title": f"{name} по компаниям", "subtitle": p.label}
    r = await metrics.metric(session, ctx.actor, company, b["metric"], p)
    return {"chart": {"kind": b.get("kind", "bar"), "labels": labels, "unit": r.unit, "compare": False,
                      "datasets": [{"label": "План", "data": [pt.plan for pt in r.series], "role": "plan"},
                                   {"label": "Факт", "data": [pt.fact for pt in r.series], "role": "fact"}]},
            "default_title": f"{r.name}: план и факт", "subtitle": p.label}


async def _kanban(session, ctx, company, b):
    dept = await refs.get_department(session, b["department"])
    board = await kanban.board(session, ctx.actor, company, dept.code)
    cards = [c for col in board.values() for c in col]
    names = {e.id: e.full_name for e in [await session.get(Employee, i) for i in
                                          {c.assignee_id for c in cards if c.assignee_id}] if e}
    comps = {c.id: c.name for c in await refs.list_companies(session)}
    today = today_local()
    columns = []
    for st, col in board.items():
        columns.append({"status": st.value, "label": STATUS_LABELS[st], "cards": [{
            "id": c.id, "title": c.title, "description": c.description,
            "assignee": names.get(c.assignee_id), "company": comps.get(c.company_id),
            "priority": c.priority, "priority_label": PRIORITY_LABELS[c.priority],
            "due": fmt_date(c.due_date) if c.due_date else None,
            "overdue": bool(c.due_date and c.due_date < today and st.value != "done"),
            "movable": can_write_kanban(ctx.actor, dept.code, c.company_id),
        } for c in col]})
    return {"columns": columns, "show_company": company == "holding", "default_title": f"Канбан · {dept.name}"}


async def _summary(session, ctx, company, b):
    rows = await metrics.summary_metrics(session, ctx.actor, company, ctx.period)
    depts = await refs.list_departments(session)
    wanted = b.get("departments") or [d.code for d in depts]
    groups = []
    for d in depts:
        if d.code in wanted:
            items = [row_view(r) for r in rows if r.department == d.code]
            if items:
                groups.append({"name": d.name, "items": items})
    return {"groups": groups, "default_title": "Сводка"}


_RENDER = {"kpis": _kpis, "table": _table, "chart": _chart, "kanban": _kanban, "summary": _summary}


async def build(session: AsyncSession, ctx: ViewContext, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, b in enumerate(blocks):
        view: dict[str, Any] = {"id": f"b{i}", **b}
        fn = _RENDER.get(b["type"])
        if fn:
            company = b.get("company") or ctx.company
            try:
                view.update(await fn(session, ctx, company, b))
            except ServiceError as e:
                view["error"] = str(e)
        view["title"] = b.get("title") or view.get("default_title")
        out.append(view)
    return out


