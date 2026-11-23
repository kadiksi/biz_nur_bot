"""Регулярные задачи (cron). Управляет только владелец; запускать будет APScheduler (шаг 7)."""
from datetime import UTC, datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, Department, Employee, RecurringTask
from app.services import changelog, refs
from app.services.access import can_read_department, require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import NotFound, ValidationError
from app.services.format import fmt_dt
from app.timeutil import local_tz, to_local, utcnow

TASK_FIELDS = ["title", "description", "cron", "company_id", "department_id", "assignee_id", "is_active", "next_run_at"]


_DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _crontab_dow(field: str) -> str:
    """crontab: 0/7 — воскресенье, 1 — понедельник. В APScheduler 3 числа значат другое
    (0 — понедельник), поэтому числа переводим в названия дней."""
    if field == "*":
        return field
    out: list[str] = []
    for part in field.split(","):
        base, _, step = part.partition("/")
        if not (base == "*" or base.replace("-", "").isdigit()):
            out.append(part)  # уже названия (mon-fri) — APScheduler понимает их правильно
            continue
        if base == "*":
            lo, hi = 0, 6
        elif "-" in base:
            lo, hi = (int(x) for x in base.split("-", 1))
        else:
            lo = hi = int(base)
        if not (0 <= lo <= 7 and 0 <= hi <= 7 and lo <= hi):
            raise ValueError(f"день недели вне диапазона 0–7: {part}")
        out.extend(_DOW[n] for n in range(lo, hi + 1, int(step or 1)))
    return ",".join(dict.fromkeys(out))


def parse_cron(cron: str) -> CronTrigger:
    try:
        fields = cron.split()
        if len(fields) != 5:
            raise ValueError("нужно 5 полей")
        minute, hour, day, month, dow = fields
        return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                           day_of_week=_crontab_dow(dow), timezone=local_tz())
    except (ValueError, AttributeError) as e:
        raise ValidationError(
            f"Неверное расписание «{cron}»: {e}. Формат cron: «минута час день месяц день_недели», например «0 10 * * 1»"
        ) from None


def next_run(cron: str, now: datetime | None = None) -> datetime | None:
    """Следующий запуск в naive UTC."""
    nxt = parse_cron(cron).get_next_fire_time(None, to_local(now or utcnow()))
    return nxt.astimezone(UTC).replace(tzinfo=None) if nxt else None


async def _company_name(session: AsyncSession, company_id: int | None) -> str:
    if company_id is None:
        return "все компании"
    c = await session.get(Company, company_id)
    return c.name


async def _dept_name(session: AsyncSession, department_id: int) -> str:
    return (await session.get(Department, department_id)).name


async def _emp_name(session: AsyncSession, emp_id: int | None) -> str:
    if emp_id is None:
        return "не назначен"
    e = await session.get(Employee, emp_id)
    return e.full_name if e else f"#{emp_id}"


async def list_tasks(session: AsyncSession, actor: Actor, department: str | None = None) -> list[RecurringTask]:
    q = select(RecurringTask).order_by(RecurringTask.id)
    if department:
        q = q.where(RecurringTask.department_id == (await refs.get_department(session, department)).id)
    tasks = list(await session.scalars(q))
    if actor.is_owner:
        return tasks
    depts = {d.id: d.code for d in await refs.list_departments(session)}
    companies = [c.id for c in await refs.list_companies(session)]

    def visible(t: RecurringTask) -> bool:
        ids = [t.company_id] if t.company_id else companies
        return any(can_read_department(actor, depts[t.department_id], cid) for cid in ids)

    return [t for t in tasks if visible(t)]


async def _resolve(session, company, department, assignee_id) -> tuple[int | None, int]:
    company_id = None if company in (None, refs.HOLDING) else (await refs.get_company(session, company)).id
    department_id = (await refs.get_department(session, department)).id
    if assignee_id is not None:
        emp = await refs.get_employee(session, assignee_id)
        if not emp.is_active:
            raise ValidationError(f"{emp.full_name} отключён(а), назначить нельзя")
    return company_id, department_id


@write_op("recurring.create_task")
async def create_task(
    session: AsyncSession,
    actor: Actor,
    *,
    title: str,
    cron: str,
    department: str,
    company: str | None = None,
    assignee_id: int | None = None,
    description: str | None = None,
    is_active: bool = True,
    dry_run: bool = False,
) -> ChangePreview:
    require_owner(actor, "создавать регулярные задачи")
    title = (title or "").strip()
    if not title:
        raise ValidationError("Название задачи не может быть пустым")
    nxt = next_run(cron)
    company_id, department_id = await _resolve(session, company, department, assignee_id)
    preview = ChangePreview(
        title="Новая регулярная задача",
        changes=[
            FieldChange("Название", None, title),
            FieldChange("Отдел", None, await _dept_name(session, department_id)),
            FieldChange("Компания", None, await _company_name(session, company_id)),
            FieldChange("Расписание", None, f"{cron} (следующий запуск {fmt_dt(nxt)})"),
            FieldChange("Исполнитель", None, await _emp_name(session, assignee_id)),
            FieldChange("Активна", None, "да" if is_active else "нет"),
        ],
    )
    if dry_run:
        return preview
    task = RecurringTask(
        title=title, description=description, cron=cron.strip(), company_id=company_id,
        department_id=department_id, assignee_id=assignee_id, is_active=is_active,
        next_run_at=nxt if is_active else None, created_by_id=actor.employee_id,
    )
    session.add(task)
    await session.flush()
    await changelog.log(session, actor, entity="recurring_task", entity_id=task.id, action="create",
                        summary=preview.render(), after=changelog.snapshot(task, TASK_FIELDS))
    await session.commit()
    preview.entity_id = task.id
    return preview


@write_op("recurring.update_task")
async def update_task(
    session: AsyncSession,
    actor: Actor,
    *,
    task_id: int,
    title: str = UNSET,
    cron: str = UNSET,
    department: str = UNSET,
    company: str | None = UNSET,
    assignee_id: int | None = UNSET,
    description: str | None = UNSET,
    is_active: bool = UNSET,
    dry_run: bool = False,
) -> ChangePreview:
    require_owner(actor, "менять регулярные задачи")
    task = await session.get(RecurringTask, task_id)
    if task is None:
        raise NotFound(f"Регулярная задача #{task_id} не найдена")

    new: dict[str, Any] = {}
    changes: list[FieldChange] = []
    if title is not UNSET and title.strip() and title.strip() != task.title:
        new["title"] = title.strip()
        changes.append(FieldChange("Название", task.title, new["title"]))
    if cron is not UNSET and cron.strip() != task.cron:
        parse_cron(cron)
        new["cron"] = cron.strip()
        changes.append(FieldChange("Расписание", task.cron, f"{new['cron']} (следующий запуск {fmt_dt(next_run(cron))})"))
    if department is not UNSET:
        dep_id = (await refs.get_department(session, department)).id
        if dep_id != task.department_id:
            new["department_id"] = dep_id
            changes.append(FieldChange("Отдел", await _dept_name(session, task.department_id), await _dept_name(session, dep_id)))
    if company is not UNSET:
        cid = None if company in (None, refs.HOLDING) else (await refs.get_company(session, company)).id
        if cid != task.company_id:
            new["company_id"] = cid
            changes.append(FieldChange("Компания", await _company_name(session, task.company_id), await _company_name(session, cid)))
    if assignee_id is not UNSET and assignee_id != task.assignee_id:
        await _resolve(session, None, (await session.get(Department, new.get("department_id", task.department_id))).code, assignee_id)
        new["assignee_id"] = assignee_id
        changes.append(FieldChange("Исполнитель", await _emp_name(session, task.assignee_id), await _emp_name(session, assignee_id)))
    if description is not UNSET and (description or None) != task.description:
        new["description"] = description or None
        changes.append(FieldChange("Описание", task.description or "—", new["description"] or "—"))
    if is_active is not UNSET and bool(is_active) != task.is_active:
        new["is_active"] = bool(is_active)
        changes.append(FieldChange("Активна", "да" if task.is_active else "нет", "да" if is_active else "нет"))
    if not changes:
        raise ValidationError("Нечего менять: новые значения совпадают с текущими")

    preview = ChangePreview(title=f"Изменение регулярной задачи «{task.title}»", changes=changes)
    if dry_run:
        return preview
    before = changelog.snapshot(task, TASK_FIELDS)
    for k, v in new.items():
        setattr(task, k, v)
    task.next_run_at = next_run(task.cron) if task.is_active else None
    await changelog.log(session, actor, entity="recurring_task", entity_id=task.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(task, TASK_FIELDS))
    await session.commit()
    preview.entity_id = task.id
    return preview


@write_op("recurring.delete_task")
async def delete_task(session: AsyncSession, actor: Actor, *, task_id: int, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "удалять регулярные задачи")
    task = await session.get(RecurringTask, task_id)
    if task is None:
        raise NotFound(f"Регулярная задача #{task_id} не найдена")
    preview = ChangePreview(
        title="Удаление регулярной задачи",
        changes=[FieldChange("Задача", f"«{task.title}» ({task.cron})", None)],
    )
    if dry_run:
        return preview
    before = changelog.snapshot(task, TASK_FIELDS)
    await session.delete(task)
    await changelog.log(session, actor, entity="recurring_task", entity_id=task_id, action="delete",
                        summary=preview.render(), before=before)
    await session.commit()
    preview.entity_id = task_id
    return preview
