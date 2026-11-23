"""Факты из файлов владельца (Excel, CSV): для показателей, которых нет в 1С.

Факты из 1С здесь не меняются: если у показателя для компании есть источник в 1С
или месяц уже загружен из 1С, запись отклоняется.
"""
from __future__ import annotations

import math
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Aggregation, MetricValue, OneCSource, SyncState, SyncStatus
from app.services import changelog, refs
from app.services.access import require_owner
from app.services.changes import ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import ValidationError
from app.services.format import fmt_value, month_label
from app.services.periods import parse_month
from app.timeutil import utcnow

MAX_MONTHS = 120
SOURCE = "file"


def _values(values: dict | list) -> dict[date, float]:
    items = values.items() if isinstance(values, dict) else [(v.get("month"), v.get("value")) for v in values or []]
    out: dict[date, float] = {}
    for month, value in items:
        period = parse_month(month)
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"Факт за {month} должен быть числом, получено «{value}»") from None
        if not math.isfinite(v):
            raise ValidationError(f"Факт за {month} — не число")
        out[period] = v
    if not out:
        raise ValidationError("Нет ни одного значения")
    if len(out) > MAX_MONTHS:
        raise ValidationError(f"Не больше {MAX_MONTHS} месяцев за раз")
    return dict(sorted(out.items()))


@write_op("facts.set_facts")
async def set_facts(session: AsyncSession, actor: Actor, *, company: str, metric: str, values: dict | list,
                    source_note: str | None = None, dry_run: bool = False) -> ChangePreview:
    """values: {"2026-10": 1200000, ...} или [{"month": "2026-10", "value": 1200000}, ...]."""
    require_owner(actor, "вносить факты")
    comp = await refs.get_company(session, company)
    mdef = await refs.get_metric(session, metric)
    if mdef.aggregation == Aggregation.DERIVED:
        raise ValidationError(f"«{mdef.name}» считается по формуле ({mdef.formula}) — вносите исходные показатели")
    if await session.scalar(select(OneCSource.id).where(OneCSource.metric_id == mdef.id,
                                                        OneCSource.company_id == comp.id)):
        raise ValidationError(f"«{mdef.name}» для {comp.name} берётся из 1С — из файла его не меняю")
    vals = _values(values)
    rows = {r.period: r for r in await session.scalars(select(MetricValue).where(
        MetricValue.company_id == comp.id, MetricValue.metric_id == mdef.id, MetricValue.period.in_(list(vals))))}
    from_1c = [month_label(p) for p, r in rows.items() if r.source == "1c" and r.fact is not None]
    if from_1c:
        raise ValidationError(f"За {', '.join(from_1c)} факт «{mdef.name}» уже из 1С — его не меняю")

    fmt = lambda v: fmt_value(v, mdef.unit, mdef.decimals)  # noqa: E731
    changes = [FieldChange(month_label(p), fmt(rows[p].fact) if p in rows and rows[p].fact is not None else None, fmt(v))
               for p, v in vals.items()]
    note = (source_note or "").strip()[:200]
    title = f"Факт «{mdef.name}» · {comp.name} · {len(vals)} мес." + (f" (из {note})" if note else "")
    preview = ChangePreview(title=title, changes=changes)
    if dry_run:
        return preview

    now = utcnow()
    for p, v in vals.items():
        row = rows.get(p)
        if row is None:
            session.add(MetricValue(company_id=comp.id, metric_id=mdef.id, period=p, fact=v, source=SOURCE))
        else:
            row.fact, row.source, row.updated_at = v, SOURCE, now
    st = await session.scalar(select(SyncStatus).where(SyncStatus.source == SOURCE, SyncStatus.company_id == comp.id,
                                                        SyncStatus.department_id == mdef.department_id))
    if st is None:
        st = SyncStatus(source=SOURCE, company_id=comp.id, department_id=mdef.department_id)
        session.add(st)
    st.status, st.last_success_at, st.last_attempt_at, st.message = SyncState.OK, now, now, (note or None)
    await changelog.log(session, actor, entity="metric_value", entity_id=None, action="set_facts",
                        summary=preview.render(),
                        after={"company": comp.code, "metric": mdef.code,
                               "values": {p.isoformat(): v for p, v in vals.items()}})
    await session.commit()
    return preview
