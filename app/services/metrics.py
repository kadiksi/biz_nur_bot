"""Чтение показателей план/факт с агрегацией по месяцам и компаниям (холдинг).

Производные метрики (средний чек, CPL, ROMI, маржа…) не хранятся: формула
считается от уже агрегированных базовых. Поэтому средний чек холдинга =
Σвыручки / Σзаказов, а не среднее чеков компаний.
"""
from __future__ import annotations

import ast
import operator
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Aggregation, Department, MetricDefinition, MetricValue
from app.services import refs
from app.services.access import can_read_department, can_read_summary, require
from app.services.context import Actor
from app.services.errors import ValidationError
from app.services.periods import Period

# ---------- формулы ----------

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


class _Missing(Exception):
    pass


def formula_names(formula: str) -> set[str]:
    return {n.id for n in ast.walk(ast.parse(formula, mode="eval")) if isinstance(n, ast.Name)}


def eval_formula(formula: str, values: dict[str, float | None]) -> float | None:
    """Только числа, имена метрик, + - * / и унарный минус. Нет данных или деление на 0 → None."""

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.Name):
            v = values.get(node.id)
            if v is None:
                raise _Missing
            return v
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise _Missing
            return _BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise ValidationError(f"Недопустимое выражение в формуле: {formula}")

    try:
        return ev(ast.parse(formula, mode="eval"))
    except _Missing:
        return None


def completion(plan: float | None, fact: float | None, higher_is_better: bool) -> float | None:
    """% выполнения плана. Для «меньше — лучше» (расходы, CPL) — plan/fact."""
    if plan is None or fact is None:
        return None
    if higher_is_better:
        return fact / plan * 100 if plan else None
    return plan / fact * 100 if fact else None


# ---------- результат ----------


@dataclass
class MonthPoint:
    period: date
    plan: float | None
    fact: float | None


@dataclass
class MetricRow:
    code: str
    name: str
    department: str
    unit: str
    decimals: int
    aggregation: Aggregation
    higher_is_better: bool
    plan: float | None
    fact: float | None
    completion: float | None
    is_key: bool = False
    is_visible: bool = True
    series: list[MonthPoint] = field(default_factory=list)

    @property
    def deviation(self) -> float | None:
        if self.plan is None or self.fact is None:
            return None
        return self.fact - self.plan


# ---------- агрегация ----------


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _sum(xs: list[float]) -> float | None:
    return sum(xs) if xs else None


def _over_companies(agg: Aggregation, xs: list[float]) -> float | None:
    return _mean(xs) if agg == Aggregation.AVG else _sum(xs)


def _over_months(agg: Aggregation, xs: list[float | None]) -> float | None:
    present = [x for x in xs if x is not None]
    if agg == Aggregation.SUM:
        return _sum(present)
    if agg == Aggregation.AVG:
        return _mean(present)
    if agg == Aggregation.LAST:
        return next((x for x in reversed(xs) if x is not None), None)
    raise AssertionError(agg)


async def _definitions(session: AsyncSession) -> list[tuple[MetricDefinition, str]]:
    rows = await session.execute(
        select(MetricDefinition, Department.code)
        .join(Department, Department.id == MetricDefinition.department_id)
        .order_by(Department.sort_order, MetricDefinition.sort_order)
    )
    return [(m, d) for m, d in rows]


async def _compute(
    session: AsyncSession, company_ids: list[int], period: Period
) -> list[MetricRow]:
    defs = await _definitions(session)
    by_id = {m.id: m for m, _ in defs}
    months = period.months()

    raw: dict[str, dict[date, tuple[list[float], list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: ([], []))
    )
    result = await session.execute(
        select(MetricValue.metric_id, MetricValue.period, MetricValue.plan, MetricValue.fact).where(
            MetricValue.company_id.in_(company_ids),
            MetricValue.period >= period.start,
            MetricValue.period <= period.end,
        )
    )
    for metric_id, per, plan, fact in result:
        plans, facts = raw[by_id[metric_id].code][per]
        if plan is not None:
            plans.append(plan)
        if fact is not None:
            facts.append(fact)

    # период с точными датами: неполные крайние месяцы — факт прямо из 1С за эти дни, план — пропорционально дням
    missing: set[tuple[str, date]] = set()
    if period.is_partial:
        from app.services import onec  # здесь, чтобы не было циклического импорта

        edge = [mo for mo in months if period.coverage(mo) < 1]
        live = await onec.live_facts(session, company_ids, [period.month_range(mo) for mo in edge],
                                     {m.id for m, _ in defs if m.aggregation != Aggregation.DERIVED})
        for m, _ in defs:
            if m.aggregation == Aggregation.DERIVED:
                continue
            for mo in edge:
                plans, facts = raw[m.code][mo]
                if m.aggregation == Aggregation.SUM:
                    k = period.coverage(mo)
                    plans[:] = [p * k for p in plans]
                if m.aggregation == Aggregation.LAST and mo != months[-1]:
                    continue  # остаток важен только на конец периода
                a, _b = period.month_range(mo)
                vals = [live.get((cid, m.id, a)) for cid in company_ids]
                if vals and all(isinstance(v, int | float) for v in vals):
                    facts[:] = vals
                else:
                    facts.clear()
                    missing.add((m.code, mo))

    # базовые: сначала сводим компании внутри месяца, потом месяцы
    monthly: dict[str, list[tuple[float | None, float | None]]] = {}
    totals: dict[str, tuple[float | None, float | None]] = {}
    for m, _ in defs:
        if m.aggregation == Aggregation.DERIVED:
            continue
        series = [
            (_over_companies(m.aggregation, raw[m.code][mo][0]), _over_companies(m.aggregation, raw[m.code][mo][1]))
            for mo in months
        ]
        monthly[m.code] = series
        fact_total = _over_months(m.aggregation, [f for _, f in series])
        # нет точного факта за неполный месяц — итог за период неизвестен (не подменяем частью)
        if any((m.code, mo) in missing for mo in months) and (
                m.aggregation != Aggregation.LAST or (m.code, months[-1]) in missing):
            fact_total = None
        totals[m.code] = (_over_months(m.aggregation, [p for p, _ in series]), fact_total)

    def derived(formula: str, idx: int | None) -> tuple[float | None, float | None]:
        src = totals if idx is None else {k: v[idx] for k, v in monthly.items()}
        return (
            eval_formula(formula, {k: v[0] for k, v in src.items()}),
            eval_formula(formula, {k: v[1] for k, v in src.items()}),
        )

    rows: list[MetricRow] = []
    for m, dept in defs:
        if m.aggregation == Aggregation.DERIVED:
            plan, fact = derived(m.formula, None)
            series = [MonthPoint(mo, *derived(m.formula, i)) for i, mo in enumerate(months)]
        else:
            plan, fact = totals[m.code]
            series = [MonthPoint(mo, p, f) for mo, (p, f) in zip(months, monthly[m.code], strict=True)]
        rows.append(
            MetricRow(
                code=m.code,
                name=m.name,
                department=dept,
                unit=m.unit,
                decimals=m.decimals,
                aggregation=m.aggregation,
                higher_is_better=m.higher_is_better,
                plan=plan,
                fact=fact,
                completion=completion(plan, fact, m.higher_is_better),
                is_key=m.is_key,
                is_visible=m.is_visible,
                series=series,
            )
        )
    return rows


# ---------- публичное API ----------


async def department_metrics(
    session: AsyncSession, actor: Actor, company: str, department: str, period: Period
) -> list[MetricRow]:
    """Показатели раздела. company — код компании или 'holding'."""
    dept = await refs.get_department(session, department)
    companies = await refs.resolve_companies(session, company)
    for c in companies:
        require(can_read_department(actor, dept.code, c.id), f"Нет доступа к разделу «{dept.name}» компании {c.name}")
    rows = await _compute(session, [c.id for c in companies], period)
    return [r for r in rows if r.department == dept.code and r.is_visible]


async def summary_metrics(session: AsyncSession, actor: Actor, company: str, period: Period) -> list[MetricRow]:
    """Ключевые показатели всех отделов для раздела «Сводка»."""
    companies = await refs.resolve_companies(session, company)
    for c in companies:
        require(can_read_summary(actor, c.id), f"Нет доступа к данным компании {c.name}")
    rows = await _compute(session, [c.id for c in companies], period)
    return [r for r in rows if r.is_key]


async def metric(session: AsyncSession, actor: Actor, company: str, code: str, period: Period) -> MetricRow:
    """Один показатель (для ботов). Ключевые доступны всем в пределах компании, остальные — по отделу."""
    mdef = await refs.get_metric(session, code)
    dept = await session.get(Department, mdef.department_id)
    companies = await refs.resolve_companies(session, company)
    for c in companies:
        ok = can_read_department(actor, dept.code, c.id) or (mdef.is_key and can_read_summary(actor, c.id))
        require(ok, f"Нет доступа к показателю «{mdef.name}» компании {c.name}")
    rows = await _compute(session, [c.id for c in companies], period)
    return next(r for r in rows if r.code == code)
