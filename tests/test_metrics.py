import pytest

from app.models import Channel
from app.services import metrics
from app.services.context import Actor
from app.services.errors import ValidationError
from app.services.metrics import completion, eval_formula
from app.services.periods import Period
from tests.conftest import TODAY

SYS = Actor.system(Channel.SCRIPT)
HALF = Period.parse("half", TODAY)


def by_code(rows):
    return {r.code: r for r in rows}


async def _metric(session, company, code, period=HALF):
    return await metrics.metric(session, SYS, company, code, period)


async def test_holding_sum_equals_companies(session):
    for code in ["revenue", "orders", "net_profit", "leads", "stock_value", "cash"]:
        parts = [await _metric(session, c, code) for c in ("fler", "remely", "velse")]
        total = await _metric(session, "holding", code)
        assert total.fact == pytest.approx(sum(p.fact for p in parts)), code
        assert total.plan == pytest.approx(sum(p.plan for p in parts)), code


async def test_holding_avg_check_is_weighted_not_mean(session):
    rev = await _metric(session, "holding", "revenue")
    orders = await _metric(session, "holding", "orders")
    avg = await _metric(session, "holding", "avg_check")
    assert avg.fact == pytest.approx(rev.fact / orders.fact)
    assert avg.plan == pytest.approx(rev.plan / orders.plan)
    naive = sum([(await _metric(session, c, "avg_check")).fact for c in ("fler", "remely", "velse")]) / 3
    assert avg.fact != pytest.approx(naive, rel=1e-3)


async def test_lower_is_better_completion_inverted(session):
    cpl = await _metric(session, "fler", "cpl")
    assert not cpl.higher_is_better
    assert cpl.completion == pytest.approx(cpl.plan / cpl.fact * 100)
    assert completion(100, 80, higher_is_better=False) == pytest.approx(125)
    assert completion(100, 80, higher_is_better=True) == pytest.approx(80)
    assert completion(0, 5, True) is None and completion(5, 0, False) is None


async def test_last_metric_takes_end_of_period(session):
    stock = await _metric(session, "fler", "stock_value")
    assert stock.fact == stock.series[-1].fact
    revenue = await _metric(session, "fler", "revenue")
    assert revenue.fact == pytest.approx(sum(p.fact for p in revenue.series))


async def test_series_covers_every_month(session):
    r = await _metric(session, "holding", "revenue")
    assert [p.period.month for p in r.series] == [6, 7, 8, 9, 10, 11]
    assert all(p.plan and p.fact for p in r.series)


async def test_department_hides_base_only_metrics(session):
    rows = by_code(await metrics.department_metrics(session, SYS, "fler", "sales", HALF))
    assert "deals_from_leads" not in rows
    assert {"revenue", "orders", "avg_check", "lead_conversion"} <= set(rows)
    assert rows["lead_conversion"].fact is not None  # использует leads из маркетинга


async def test_summary_has_key_metrics_of_all_departments(session):
    rows = await metrics.summary_metrics(session, SYS, "holding", Period.parse("month", TODAY))
    assert {r.department for r in rows} == {"sales", "marketing", "warehouse", "finance"}


async def test_current_month_fact_is_prorated(session):
    nov = await _metric(session, "fler", "revenue", Period.parse("month", TODAY))
    oct_ = await _metric(session, "fler", "revenue", Period.parse("prev_month", TODAY))
    assert nov.completion < 85 < oct_.completion + 30  # 21 из 30 дней ≈ 70 %


def test_formula_evaluator():
    assert eval_formula("a / b * 100", {"a": 1, "b": 4}) == 25
    assert eval_formula("(a - b) / b", {"a": 3, "b": 2}) == 0.5
    assert eval_formula("a / b", {"a": 1, "b": 0}) is None
    assert eval_formula("a / b", {"a": 1, "b": None}) is None
    for bad in ["__import__('os')", "a.b", "a ** 2"]:
        with pytest.raises(ValidationError):
            eval_formula(bad, {"a": 1, "b": 1})


def test_period_parsing():
    assert Period.parse("quarter", TODAY).label == "октябрь–ноябрь 2026"
    assert Period.parse("ytd", TODAY).start.month == 1
    assert len(Period.parse("2026-01..2026-06").months()) == 6
    for bad in ["2026-13", "2026-06..2026-01", "вчера"]:
        with pytest.raises(ValidationError):
            Period.parse(bad, TODAY)
