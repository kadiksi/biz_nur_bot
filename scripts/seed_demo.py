"""Демо-данные: 3 компании × 6 месяцев, сотрудники, канбан, регулярные задачи, sync_status.

    python -m scripts.seed_demo            # заполнить пустую базу
    python -m scripts.seed_demo --reset    # пересоздать все таблицы и заполнить заново

Пишет напрямую в БД (это начальная загрузка, а не пользовательское изменение);
в change_log остаётся одна запись «seed».
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import random
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog
from app.config import settings
from app.db import create_tables, get_engine, get_sessionmaker
from app.models import (
    CardStatus,
    ChangeLog,
    Channel,
    Company,
    Department,
    Employee,
    EmployeeAccess,
    KanbanCard,
    MetricDefinition,
    MetricValue,
    RecurringTask,
    Role,
    SyncState,
    SyncStatus,
)
from app.services.periods import add_months, month_start, today_local
from app.services.recurring import next_run
from app.timeutil import utcnow

PLACEHOLDER_OWNER_ID = 100000001
MONTHS = 6

# масштаб бизнеса, средний чек, оборачиваемость
PROFILES = {
    "fler": dict(scale=1.0, avg_check=8500, turnover=38),
    "remely": dict(scale=0.6, avg_check=6200, turnover=42),
    "velse": dict(scale=0.35, avg_check=11000, turnover=47),
}
# сезонность по номеру месяца
SEASON = {1: 0.85, 2: 0.88, 3: 0.95, 4: 0.97, 5: 0.95, 6: 0.92, 7: 0.88, 8: 0.95,
          9: 1.02, 10: 1.08, 11: 1.15, 12: 1.30}
BASE_REVENUE = 24_000_000

EMPLOYEES = [
    # telegram_id, имя, роль, отдел, компания (None — все), активен
    (900000001, "Анна Соколова", Role.HEAD, "sales", None, True),
    (900000002, "Илья Петров", Role.EMPLOYEE, "sales", "fler", True),
    (900000003, "Мария Кузнецова", Role.HEAD, "marketing", None, True),
    (900000004, "Дмитрий Орлов", Role.EMPLOYEE, "marketing", "remely", True),
    (900000005, "Сергей Волков", Role.HEAD, "warehouse", None, True),
    (900000006, "Ольга Белова", Role.EMPLOYEE, "warehouse", "velse", True),
    (900000007, "Елена Морозова", Role.HEAD, "finance", None, True),
    (900000008, "Павел Никитин", Role.EMPLOYEE, "finance", None, True),
    (900000009, "Кирилл Зайцев", Role.EMPLOYEE, "sales", "remely", False),
]
# Дмитрий (маркетинг Remely) дополнительно видит ОП FLÉR — пример гранта
GRANTS = [(900000004, "sales", "fler", False)]

CARDS = {
    "sales": ["Обзвонить спящих клиентов", "Подготовить КП для сети магазинов", "Акция к Чёрной пятнице",
              "Обновить скрипт продаж", "Разобрать возвраты за октябрь", "Встреча с оптовым клиентом",
              "Настроить воронку в CRM", "План продаж на декабрь", "Обучение новых менеджеров"],
    "marketing": ["Запустить рекламу в VK", "Контент-план на декабрь", "A/B тест лендинга",
                  "Отчёт по ROMI за квартал", "Новогодняя email-рассылка", "Съёмка нового каталога",
                  "Отключить неэффективные кампании", "Коллаборация с блогерами", "Обновить карточки на маркетплейсах"],
    "warehouse": ["Инвентаризация склада", "Поставка от основного поставщика", "Разобрать неликвид",
                  "Оптимизировать зону комплектации", "Проверить сроки годности", "Договор с новой ТК",
                  "Маркировка новой партии", "Ремонт стеллажей", "Отчёт по списаниям"],
    "finance": ["Платёжный календарь на декабрь", "Сверка с поставщиками", "Закрыть ноябрь в 1С",
                "Отчёт по дебиторке", "Бюджет на 2027 год", "Проверить налоговые платежи",
                "Кредитная линия: продление", "Анализ маржинальности по SKU", "Оптимизация расходов на аренду"],
}

RECURRING = [
    ("Еженедельный отчёт по продажам", "0 10 * * 1", "sales", None),
    ("Отчёт по рекламным кампаниям", "0 11 * * 2", "marketing", None),
    ("Сверка остатков", "0 9 * * 5", "warehouse", None),
    ("Платёжный календарь на неделю", "0 9 * * 1", "finance", None),
    ("Закрытие месяца", "0 10 1 * *", "finance", None),
    ("Проверка дебиторки FLÉR", "0 15 * * 3", "finance", "fler"),
]


def _metric_values(rng: random.Random, prof: dict, month: date, prorate: float) -> dict[str, tuple[float, float]]:
    """plan/fact для всех базовых метрик одной компании за месяц. Факты согласованы между собой."""
    n = lambda lo=0.95, hi=1.05: rng.uniform(lo, hi)  # noqa: E731
    season = SEASON[month.month]
    rev_p = BASE_REVENUE * prof["scale"] * season
    rev_f = rev_p * rng.uniform(0.84, 1.10)
    chk = prof["avg_check"]
    orders_p, orders_f = rev_p / chk, rev_f / (chk * n(0.94, 1.06))
    leads_p, leads_f = orders_p * 1.9, orders_f * 1.9 * n(0.88, 1.12)
    budget_p, budget_f = rev_p * 0.07, rev_p * 0.07 * n(0.92, 1.12)
    ship_p, ship_f = orders_p * 0.97, orders_f * 0.97 * n(0.98, 1.0)
    gp_p, gp_f = rev_p * 0.46, rev_f * n(0.43, 0.48)
    opex_p, opex_f = rev_p * 0.29, rev_p * 0.29 * n(0.93, 1.08)

    flows = {  # суммируемые за месяц — для текущего месяца факт пропорционален прошедшим дням
        "revenue": (rev_p, rev_f),
        "orders": (orders_p, orders_f),
        "new_clients": (orders_p * 0.28, orders_f * 0.28 * n(0.85, 1.15)),
        "deals_from_leads": (leads_p * 0.22, leads_f * 0.22 * n(0.85, 1.12)),
        "marketing_budget": (budget_p, budget_f),
        "leads": (leads_p, leads_f),
        "attributed_revenue": (rev_p * 0.35, rev_f * 0.35 * n(0.85, 1.15)),
        "traffic": (leads_p * 28, leads_f * 28 * n(0.9, 1.1)),
        "shipments": (ship_p, ship_f),
        "shipments_on_time": (ship_p * 0.96, ship_f * rng.uniform(0.90, 0.985)),
        "writeoffs": (rev_p * 0.006, rev_p * 0.006 * n(0.6, 1.5)),
        "gross_profit": (gp_p, gp_f),
        "opex": (opex_p, opex_f),
        "net_profit": (gp_p - opex_p - rev_p * 0.03, gp_f - opex_f - rev_f * 0.03),
    }
    stocks = {  # на дату — не пропорционируются
        "stock_value": (rev_p * 1.3, rev_p * 1.3 * n(0.9, 1.2)),
        "turnover_days": (prof["turnover"], prof["turnover"] * n(0.9, 1.2)),
        "receivables": (rev_p * 0.18, rev_p * 0.18 * n(0.85, 1.3)),
        "cash": (rev_p * 0.55, rev_p * 0.55 * n(0.8, 1.15)),
    }
    out = {k: (p, f * prorate) for k, (p, f) in flows.items()}
    out.update(stocks)
    return out


def _round(code: str, v: float) -> float:
    return round(v, 1) if code == "turnover_days" else float(round(v))


async def seed(
    session: AsyncSession,
    owner_telegram_id: int | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> bool:
    """Заполняет пустую базу. Если данные уже есть — ничего не делает и возвращает False."""
    if await session.scalar(select(func.count()).select_from(Company)):
        return False
    rng = random.Random(42)
    today = today or today_local()
    now = now or utcnow()
    owner_tg = owner_telegram_id or PLACEHOLDER_OWNER_ID

    companies = {}
    for i, (code, name) in enumerate(catalog.COMPANIES):
        companies[code] = Company(code=code, name=name, sort_order=i)
    depts = {}
    for i, (code, name) in enumerate(catalog.DEPARTMENTS):
        depts[code] = Department(code=code, name=name, sort_order=i)
    session.add_all([*companies.values(), *depts.values()])
    await session.flush()

    metrics = {}
    for i, s in enumerate(catalog.METRICS):
        metrics[s.code] = MetricDefinition(
            code=s.code, name=s.name, department_id=depts[s.department].id, unit=s.unit,
            aggregation=s.aggregation, formula=s.formula, higher_is_better=s.higher_is_better,
            is_key=s.is_key, is_visible=s.is_visible, decimals=s.decimals, sort_order=i,
        )
    session.add_all(metrics.values())

    # показатели
    cur = month_start(today)
    days_in_cur = calendar.monthrange(cur.year, cur.month)[1]
    months = [add_months(cur, -i) for i in range(MONTHS - 1, -1, -1)]
    await session.flush()
    for ccode, comp in companies.items():
        for mo in months:
            prorate = today.day / days_in_cur if mo == cur else 1.0
            for code, (plan, fact) in _metric_values(rng, PROFILES[ccode], mo, prorate).items():
                session.add(MetricValue(company_id=comp.id, metric_id=metrics[code].id, period=mo,
                                        plan=_round(code, plan), fact=_round(code, fact), source="demo"))

    # сотрудники
    owner = Employee(telegram_id=owner_tg, full_name="Владелец", role=Role.OWNER)
    session.add(owner)
    emps: dict[int, Employee] = {}
    for tg, name, role, dept, comp, active in EMPLOYEES:
        emps[tg] = Employee(telegram_id=tg, full_name=name, role=role, department_id=depts[dept].id,
                            company_id=companies[comp].id if comp else None, is_active=active)
    session.add_all(emps.values())
    await session.flush()
    for tg, dept, comp, write in GRANTS:
        session.add(EmployeeAccess(employee_id=emps[tg].id, department_id=depts[dept].id,
                                   company_id=companies[comp].id if comp else None, can_write_kanban=write))

    # канбан
    statuses = list(CardStatus)
    comp_list = list(companies.values())
    positions: dict[tuple, int] = {}
    for dcode, titles in CARDS.items():
        staff = [e for tg, e in emps.items() if e.department_id == depts[dcode].id and e.is_active]
        for i, title in enumerate(titles):
            comp = comp_list[i % len(comp_list)]
            st = statuses[(i * 7 + len(dcode)) % len(statuses)]
            eligible = [e for e in staff if e.company_id in (None, comp.id)]
            key = (comp.id, dcode, st)
            positions[key] = positions.get(key, -1) + 1
            session.add(KanbanCard(
                company_id=comp.id, department_id=depts[dcode].id, title=title, status=st,
                position=positions[key], priority=rng.choice(["low", "normal", "normal", "high"]),
                assignee_id=rng.choice(eligible).id if eligible else None, created_by_id=owner.id,
                due_date=today + timedelta(days=rng.randint(-5, 30)) if st != CardStatus.DONE else None,
                created_at=now - timedelta(days=rng.randint(1, 40)),
            ))

    # регулярные задачи
    for title, cron, dcode, ccode in RECURRING:
        head = next(e for e in emps.values() if e.department_id == depts[dcode].id and e.role == Role.HEAD)
        session.add(RecurringTask(
            title=title, cron=cron, department_id=depts[dcode].id,
            company_id=companies[ccode].id if ccode else None, assignee_id=head.id,
            is_active=True, next_run_at=next_run(cron, now), created_by_id=owner.id,
        ))

    # свежесть: разная, одна ошибка
    ages = [5, 12, 40, 90, 180, 300, 25, 8, 60, 1440 + 120, 15, 3]
    for i, (ccode, comp) in enumerate(companies.items()):
        for j, (dcode, dept) in enumerate(depts.items()):
            error = ccode == "velse" and dcode == "warehouse"
            age = timedelta(days=2, hours=3) if error else timedelta(minutes=ages[(i * 4 + j) % len(ages)])
            session.add(SyncStatus(
                source="demo", company_id=comp.id, department_id=dept.id,
                status=SyncState.ERROR if error else SyncState.OK,
                last_success_at=now - age, last_attempt_at=now - timedelta(minutes=10 if error else age.seconds // 60),
                message="Демо: 1С не ответила (таймаут соединения)" if error else None,
            ))

    session.add(ChangeLog(actor_name="system:script", channel=Channel.SCRIPT, entity="database",
                          action="seed", summary=f"Демо-данные: {len(companies)} компании × {MONTHS} мес."))
    await session.commit()
    return True


async def _print_summary(session: AsyncSession) -> None:
    from app.models import Channel as Ch
    from app.services import metrics as m
    from app.services.context import Actor
    from app.services.format import fmt_value
    from app.services.periods import Period

    print("\nСтрок в таблицах:")
    for model in (Company, Department, Employee, EmployeeAccess, MetricDefinition, MetricValue,
                  KanbanCard, RecurringTask, SyncStatus, ChangeLog):
        n = await session.scalar(select(func.count()).select_from(model))
        print(f"  {model.__tablename__:<20} {n}")

    actor = Actor.system(Ch.SCRIPT)
    period = Period.parse("half")
    print(f"\nВыручка, {period.label} (план → факт, % выполнения):")
    for code in [c for c, _ in catalog.COMPANIES] + ["holding"]:
        r = await m.metric(session, actor, code, "revenue", period)
        name = "Холдинг" if code == "holding" else dict(catalog.COMPANIES)[code]
        print(f"  {name:<8} {fmt_value(r.plan, '₽'):>18} → {fmt_value(r.fact, '₽'):>18}   {r.completion:5.1f} %")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="удалить и пересоздать все таблицы")
    args = ap.parse_args()

    engine = get_engine()
    await create_tables(engine, drop=args.reset)
    if not settings.owner_telegram_id:
        print(f"⚠ OWNER_TELEGRAM_ID не задан в .env — владелец создан с временным ID {PLACEHOLDER_OWNER_ID}")
    async with get_sessionmaker()() as session:
        created = await seed(session, settings.owner_telegram_id)
        print("✓ Демо-данные загружены" if created else "• База уже заполнена — пропускаю (для пересоздания: --reset)")
        await _print_summary(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
