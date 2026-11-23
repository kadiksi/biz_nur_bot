"""Структура: отделы и показатели. Ведёт владелец через главного бота; код при этом не меняется."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (Aggregation, Department, Employee, EmployeeAccess, KanbanCard, MetricDefinition, MetricValue,
                        OneCSource, RecurringTask, SitePage, SyncStatus)
from app.services import changelog, refs
from app.services.access import require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import ValidationError
from app.services.metrics import eval_formula, formula_names

DEPT_CODE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
METRIC_CODE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
AGG_LABELS = {"sum": "сумма за период", "avg": "среднее за период", "last": "на конец периода",
              "derived": "по формуле"}
_DEPT_FIELDS = ["code", "name", "sort_order"]
_METRIC_FIELDS = ["code", "name", "unit", "aggregation", "formula", "higher_is_better", "is_key", "is_visible",
                  "decimals", "sort_order"]
_METRIC_LABELS = {"name": "Название", "unit": "Единица", "aggregation": "Как считается", "formula": "Формула",
                  "higher_is_better": "Больше — лучше", "is_key": "В сводке", "is_visible": "Показывать",
                  "decimals": "Знаков после запятой", "sort_order": "Место в списке", "department": "Отдел"}


def _text(v: Any, what: str, limit: int) -> str:
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > limit:
        raise ValidationError(f"{what}: непустой текст до {limit} символов")
    return v.strip()


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "да" if v else "нет"
    if v is None:
        return "—"
    return AGG_LABELS.get(v, str(v)) if isinstance(v, str) else str(v)


async def _blocks_using(session: AsyncSession, *, metric: str | None = None, department: str | None = None) -> list[str]:
    out = []
    for p in await session.scalars(select(SitePage)):
        if department and p.department == department:
            out.append(p.title)
            continue
        for b in p.blocks:
            if (metric and (metric in (b.get("metrics") or []) or b.get("metric") == metric)) or (
                    department and (b.get("department") == department or department in (b.get("departments") or []))):
                out.append(p.title)
                break
    return out


# ---------- отделы ----------


@write_op("departments.create")
async def create_department(session: AsyncSession, actor: Actor, *, code: str, name: str,
                            sort_order: int | None = None, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "добавлять отделы")
    code = (code or "").strip().lower()
    if not DEPT_CODE.match(code):
        raise ValidationError("Код отдела — латиница в нижнем регистре, цифры, _ или -, 2–32 символа: logistics")
    name = _text(name, "Название отдела", 60)
    if await session.scalar(select(Department.id).where((Department.code == code) | (Department.name == name))):
        raise ValidationError("Такой отдел уже есть")
    if sort_order is None:
        sort_order = (await session.scalar(select(func.max(Department.sort_order))) or -1) + 1
    preview = ChangePreview(title=f"Новый отдел «{name}»",
                            changes=[FieldChange("Код", None, code), FieldChange("Название", None, name)])
    if dry_run:
        return preview
    d = Department(code=code, name=name, sort_order=int(sort_order))
    session.add(d)
    await session.flush()
    await changelog.log(session, actor, entity="department", entity_id=d.id, action="create",
                        summary=preview.render(), after=changelog.snapshot(d, _DEPT_FIELDS))
    await session.commit()
    return preview


@write_op("departments.update")
async def update_department(session: AsyncSession, actor: Actor, *, code: str, name: str = UNSET,
                            sort_order: int = UNSET, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "менять отделы")
    d = await refs.get_department(session, code)
    new = {}
    if name is not UNSET and _text(name, "Название отдела", 60) != d.name:
        new["name"] = name.strip()
        if await session.scalar(select(Department.id).where(Department.name == new["name"], Department.id != d.id)):
            raise ValidationError("Отдел с таким названием уже есть")
    if sort_order is not UNSET and int(sort_order) != d.sort_order:
        new["sort_order"] = int(sort_order)
    if not new:
        raise ValidationError("Менять нечего")
    labels = {"name": "Название", "sort_order": "Место в списке"}
    preview = ChangePreview(title=f"Отдел «{d.name}»",
                            changes=[FieldChange(labels[k], _fmt(getattr(d, k)), _fmt(v)) for k, v in new.items()])
    if dry_run:
        return preview
    before = changelog.snapshot(d, _DEPT_FIELDS)
    for k, v in new.items():
        setattr(d, k, v)
    await changelog.log(session, actor, entity="department", entity_id=d.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(d, _DEPT_FIELDS))
    await session.commit()
    return preview


@write_op("departments.delete")
async def delete_department(session: AsyncSession, actor: Actor, *, code: str, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "удалять отделы")
    d = await refs.get_department(session, code)
    used = []
    for model, label in ((MetricDefinition, "показатели"), (Employee, "сотрудники"), (EmployeeAccess, "доступы"),
                         (KanbanCard, "карточки канбана"), (RecurringTask, "регулярные задачи"),
                         (SyncStatus, "метки синхронизации")):
        if await session.scalar(select(func.count()).select_from(model).where(model.department_id == d.id)):
            used.append(label)
    pages = await _blocks_using(session, department=d.code)
    if pages:
        used.append("страницы сайта: " + ", ".join(dict.fromkeys(pages)))
    if used:
        raise ValidationError(f"В отделе «{d.name}» есть {', '.join(used)} — сначала уберите их")
    preview = ChangePreview(title=f"Удалить отдел «{d.name}»", changes=[FieldChange("Код", d.code, None)])
    if dry_run:
        return preview
    await changelog.log(session, actor, entity="department", entity_id=d.id, action="delete",
                        summary=preview.render(), before=changelog.snapshot(d, _DEPT_FIELDS))
    await session.delete(d)
    await session.commit()
    return preview


# ---------- показатели ----------


async def _check_formula(session: AsyncSession, formula: Any, own_code: str) -> str:
    if not isinstance(formula, str) or not formula.strip():
        raise ValidationError("Для показателя по формуле нужна формула: например revenue / orders")
    formula = formula.strip()
    try:
        names = formula_names(formula)
        eval_formula(formula, {n: 1.0 for n in names})
    except SyntaxError:
        raise ValidationError(f"Формула не разобрана: {formula}") from None
    if not names:
        raise ValidationError("В формуле нет ни одного показателя")
    if own_code in names:
        raise ValidationError("Формула не может ссылаться на сам показатель")
    rows = {m.code: m for m in await session.scalars(select(MetricDefinition).where(MetricDefinition.code.in_(names)))}
    unknown = names - set(rows)
    if unknown:
        raise ValidationError(f"В формуле неизвестные показатели: {', '.join(sorted(unknown))}")
    derived = [c for c, m in rows.items() if m.aggregation == Aggregation.DERIVED]
    if derived:
        raise ValidationError(f"Формула может опираться только на исходные показатели, а {', '.join(derived)} "
                              "сами считаются формулой")
    return formula


async def _metric_values(session: AsyncSession, metric_id: int) -> int:
    return await session.scalar(select(func.count()).select_from(MetricValue).where(MetricValue.metric_id == metric_id))


@write_op("metrics.create")
async def create_metric(session: AsyncSession, actor: Actor, *, code: str, name: str, department: str, unit: str,
                        aggregation: str = "sum", formula: str | None = None, higher_is_better: bool = True,
                        is_key: bool = False, is_visible: bool = True, decimals: int = 0,
                        sort_order: int | None = None, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "добавлять показатели")
    code = (code or "").strip().lower()
    if not METRIC_CODE.match(code):
        raise ValidationError("Код показателя — латиница в нижнем регистре, цифры и _, 2–40 символов: gross_sales")
    if await session.scalar(select(MetricDefinition.id).where(MetricDefinition.code == code)):
        raise ValidationError(f"Показатель с кодом «{code}» уже есть")
    dept = await refs.get_department(session, department)
    if aggregation not in AGG_LABELS:
        raise ValidationError("Как считается: sum, avg, last или derived")
    formula = await _check_formula(session, formula, code) if aggregation == "derived" else None
    vals = {"name": _text(name, "Название показателя", 100), "unit": _text(unit, "Единица", 16),
            "aggregation": aggregation, "formula": formula, "higher_is_better": bool(higher_is_better),
            "is_key": bool(is_key), "is_visible": bool(is_visible), "decimals": max(0, min(int(decimals), 4))}
    if sort_order is None:
        sort_order = (await session.scalar(select(func.max(MetricDefinition.sort_order))
                                           .where(MetricDefinition.department_id == dept.id)) or -1) + 1
    preview = ChangePreview(
        title=f"Новый показатель «{vals['name']}» · {dept.name}",
        changes=[FieldChange("Код", None, code)] + [FieldChange(_METRIC_LABELS[k], None, _fmt(v))
                                                    for k, v in vals.items() if k != "name" and v is not None])
    if dry_run:
        return preview
    m = MetricDefinition(code=code, department_id=dept.id, sort_order=int(sort_order),
                         **{**vals, "aggregation": Aggregation(aggregation)})
    session.add(m)
    await session.flush()
    await changelog.log(session, actor, entity="metric_definition", entity_id=m.id, action="create",
                        summary=preview.render(), after=changelog.snapshot(m, _METRIC_FIELDS))
    await session.commit()
    return preview


@write_op("metrics.update")
async def update_metric(session: AsyncSession, actor: Actor, *, code: str, name: str = UNSET,
                        department: str = UNSET, unit: str = UNSET, aggregation: str = UNSET,
                        formula: str | None = UNSET, higher_is_better: bool = UNSET, is_key: bool = UNSET,
                        is_visible: bool = UNSET, decimals: int = UNSET, sort_order: int = UNSET,
                        dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "менять показатели")
    m = await refs.get_metric(session, code)
    cur_dept = await session.get(Department, m.department_id)
    new: dict[str, Any] = {}
    if name is not UNSET:
        new["name"] = _text(name, "Название показателя", 100)
    if unit is not UNSET:
        new["unit"] = _text(unit, "Единица", 16)
    agg = m.aggregation.value
    if aggregation is not UNSET:
        if aggregation not in AGG_LABELS:
            raise ValidationError("Как считается: sum, avg, last или derived")
        if aggregation == "derived" and agg != "derived" and await _metric_values(session, m.id):
            raise ValidationError(f"У «{m.name}» уже есть планы и факты — сделать его формульным нельзя: "
                                  "данные потеряют смысл. Заведите новый показатель с формулой")
        if aggregation != "derived" and agg == "derived":
            new["formula"] = None
        new["aggregation"] = agg = aggregation
    if agg == "derived" and (formula is not UNSET or aggregation is not UNSET):
        new["formula"] = await _check_formula(session, m.formula if formula is UNSET else formula, m.code)
    elif formula not in (UNSET, None):
        raise ValidationError("Формула задаётся только для показателя с aggregation=derived")
    for k, v in (("higher_is_better", higher_is_better), ("is_key", is_key), ("is_visible", is_visible)):
        if v is not UNSET:
            new[k] = bool(v)
    if decimals is not UNSET:
        new["decimals"] = max(0, min(int(decimals), 4))
    if sort_order is not UNSET:
        new["sort_order"] = int(sort_order)
    if new.get("is_visible") is False:
        pages = await _blocks_using(session, metric=m.code)
        if pages:
            raise ValidationError(f"«{m.name}» показан на страницах: {', '.join(dict.fromkeys(pages))}. "
                                  "Сначала уберите его оттуда")
    dept_change = None
    if department is not UNSET:
        d = await refs.get_department(session, department)
        if d.id != m.department_id:
            dept_change = d
    diff = [FieldChange(_METRIC_LABELS[k], _fmt(getattr(m, k).value if k == "aggregation" else getattr(m, k)),
                        _fmt(v)) for k, v in new.items()
            if (getattr(m, k).value if k == "aggregation" else getattr(m, k)) != v]
    if dept_change:
        diff.append(FieldChange("Отдел", cur_dept.name, dept_change.name))
    if not diff:
        raise ValidationError("Менять нечего")
    preview = ChangePreview(title=f"Показатель «{m.name}»", changes=diff)
    if dry_run:
        return preview
    before = changelog.snapshot(m, _METRIC_FIELDS) | {"department": cur_dept.code}
    for k, v in new.items():
        setattr(m, k, Aggregation(v) if k == "aggregation" else v)
    if dept_change:
        m.department_id = dept_change.id
    await changelog.log(session, actor, entity="metric_definition", entity_id=m.id, action="update",
                        summary=preview.render(), before=before,
                        after=changelog.snapshot(m, _METRIC_FIELDS) | {"department": (dept_change or cur_dept).code})
    await session.commit()
    return preview


@write_op("metrics.delete")
async def delete_metric(session: AsyncSession, actor: Actor, *, code: str, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "удалять показатели")
    m = await refs.get_metric(session, code)
    used = []
    if await _metric_values(session, m.id):
        used.append("планы и факты")
    if await session.scalar(select(func.count()).select_from(OneCSource).where(OneCSource.metric_id == m.id)):
        used.append("источники 1С")
    in_formulas = [x.code for x in await session.scalars(select(MetricDefinition).where(
        MetricDefinition.formula.is_not(None))) if m.code in formula_names(x.formula)]
    if in_formulas:
        used.append("формулы: " + ", ".join(in_formulas))
    pages = await _blocks_using(session, metric=m.code)
    if pages:
        used.append("страницы сайта: " + ", ".join(dict.fromkeys(pages)))
    if used:
        raise ValidationError(f"«{m.name}» используется ({'; '.join(used)}) — удалить нельзя. "
                              "Можно скрыть: is_visible=false")
    preview = ChangePreview(title=f"Удалить показатель «{m.name}»", changes=[FieldChange("Код", m.code, None)])
    if dry_run:
        return preview
    await changelog.log(session, actor, entity="metric_definition", entity_id=m.id, action="delete",
                        summary=preview.render(), before=changelog.snapshot(m, _METRIC_FIELDS))
    await session.delete(m)
    await session.commit()
    return preview
