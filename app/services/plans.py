"""Планы. Менять может только владелец (главный бот или сайт). Факты сюда не пишутся — они из 1С."""
import math
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Aggregation, MetricValue
from app.services import changelog, refs
from app.services.access import require_owner
from app.services.changes import ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import ValidationError
from app.services.format import fmt_value, month_label
from app.services.periods import parse_month
from app.timeutil import utcnow


@write_op("plans.set_plan")
async def set_plan(
    session: AsyncSession,
    actor: Actor,
    *,
    company: str,
    metric: str,
    month: str | date,
    value: float,
    dry_run: bool = False,
) -> ChangePreview:
    require_owner(actor, "менять планы")
    comp = await refs.get_company(session, company)
    mdef = await refs.get_metric(session, metric)
    if mdef.aggregation == Aggregation.DERIVED:
        raise ValidationError(
            f"«{mdef.name}» считается по формуле ({mdef.formula}) — меняйте план исходных показателей"
        )
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"План должен быть числом, получено «{value}»") from None
    if not math.isfinite(value) or value < 0:
        raise ValidationError("План должен быть неотрицательным числом")
    period = parse_month(month)

    row = await session.scalar(
        select(MetricValue).where(
            MetricValue.company_id == comp.id, MetricValue.metric_id == mdef.id, MetricValue.period == period
        )
    )
    before = row.plan if row else None
    preview = ChangePreview(
        title=f"План «{mdef.name}» · {comp.name} · {month_label(period)}",
        changes=[FieldChange("План", fmt_value(before, mdef.unit, mdef.decimals), fmt_value(value, mdef.unit, mdef.decimals))],
    )
    if dry_run:
        return preview

    if row is None:
        row = MetricValue(company_id=comp.id, metric_id=mdef.id, period=period, plan=value, source="manual")
        session.add(row)
        await session.flush()
    else:
        row.plan = value
        row.updated_at = utcnow()
    await changelog.log(
        session,
        actor,
        entity="metric_value",
        entity_id=row.id,
        action="set_plan",
        summary=preview.render(),
        before={"company": comp.code, "metric": mdef.code, "period": period, "plan": before},
        after={"company": comp.code, "metric": mdef.code, "period": period, "plan": value},
    )
    await session.commit()
    preview.entity_id = row.id
    return preview
