"""Справочники: компании, отделы, показатели. Их загружает скрипт инициализации."""
from dataclasses import dataclass

from app.models import Aggregation as A

# Только для демо-данных (scripts/seed_demo.py). Настоящие компании ведёт владелец через бота.
COMPANIES = [
    ("fler", "FLÉR"),
    ("remely", "Remely"),
    ("velse", "Velse"),
]

DEPARTMENTS = [
    ("sales", "ОП"),
    ("marketing", "Маркетинг"),
    ("warehouse", "Склад"),
    ("finance", "Финансы"),
]


@dataclass(frozen=True)
class MetricSpec:
    code: str
    department: str
    name: str
    unit: str
    aggregation: A
    formula: str | None = None
    higher_is_better: bool = True
    is_key: bool = False
    is_visible: bool = True
    decimals: int = 0


METRICS = [
    # ОП
    MetricSpec("revenue", "sales", "Выручка", "₽", A.SUM, is_key=True),
    MetricSpec("orders", "sales", "Заказы", "шт", A.SUM, is_key=True),
    MetricSpec("avg_check", "sales", "Средний чек", "₽", A.DERIVED, "revenue / orders", is_key=True),
    MetricSpec("new_clients", "sales", "Новые клиенты", "шт", A.SUM),
    MetricSpec("deals_from_leads", "sales", "Сделки из лидов", "шт", A.SUM, is_visible=False),
    MetricSpec("lead_conversion", "sales", "Конверсия лидов", "%", A.DERIVED,
               "deals_from_leads / leads * 100", decimals=1),
    # Маркетинг
    MetricSpec("marketing_budget", "marketing", "Рекламный бюджет", "₽", A.SUM, higher_is_better=False),
    MetricSpec("leads", "marketing", "Лиды", "шт", A.SUM, is_key=True),
    MetricSpec("cpl", "marketing", "Стоимость лида (CPL)", "₽", A.DERIVED,
               "marketing_budget / leads", higher_is_better=False, is_key=True),
    MetricSpec("attributed_revenue", "marketing", "Выручка с рекламы", "₽", A.SUM, is_visible=False),
    MetricSpec("romi", "marketing", "ROMI", "%", A.DERIVED,
               "(attributed_revenue - marketing_budget) / marketing_budget * 100"),
    MetricSpec("traffic", "marketing", "Трафик сайта", "визиты", A.SUM),
    # Склад
    MetricSpec("stock_value", "warehouse", "Остатки на складе", "₽", A.LAST, higher_is_better=False, is_key=True),
    MetricSpec("turnover_days", "warehouse", "Оборачиваемость", "дн", A.AVG, higher_is_better=False),
    MetricSpec("shipments", "warehouse", "Отгрузки", "шт", A.SUM),
    MetricSpec("shipments_on_time", "warehouse", "Отгрузки в срок, шт", "шт", A.SUM, is_visible=False),
    MetricSpec("on_time_rate", "warehouse", "Отгрузки в срок", "%", A.DERIVED,
               "shipments_on_time / shipments * 100", decimals=1),
    MetricSpec("writeoffs", "warehouse", "Списания", "₽", A.SUM, higher_is_better=False),
    # Финансы
    MetricSpec("gross_profit", "finance", "Валовая прибыль", "₽", A.SUM),
    MetricSpec("gross_margin", "finance", "Валовая маржа", "%", A.DERIVED,
               "gross_profit / revenue * 100", decimals=1),
    MetricSpec("opex", "finance", "Операционные расходы", "₽", A.SUM, higher_is_better=False),
    MetricSpec("net_profit", "finance", "Чистая прибыль", "₽", A.SUM, is_key=True),
    MetricSpec("receivables", "finance", "Дебиторская задолженность", "₽", A.LAST, higher_is_better=False),
    MetricSpec("cash", "finance", "Остаток денег", "₽", A.LAST, is_key=True),
]
