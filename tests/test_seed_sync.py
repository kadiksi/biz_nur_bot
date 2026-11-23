from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.models import Channel, Company, MetricValue
from app.services import sync
from app.services.context import Actor
from app.services.sync import humanize_age
from scripts.seed_demo import seed
from tests.conftest import NOW, OWNER_TG, SALES_FLER

SYS = Actor.system(Channel.SCRIPT)


async def test_seed_shape(session):
    assert await session.scalar(select(func.count()).select_from(Company)) == 3
    months = await session.scalar(select(func.count(func.distinct(MetricValue.period))))
    assert months == 6
    assert await session.scalar(select(func.count()).select_from(MetricValue)) == 3 * 6 * 18


async def test_seed_is_idempotent(session):
    n = await session.scalar(select(func.count()).select_from(MetricValue))
    assert await seed(session, OWNER_TG) is False
    assert await session.scalar(select(func.count()).select_from(MetricValue)) == n


async def test_freshness_labels(session, actor):
    ok = await sync.freshness(session, SYS, "fler", "sales", now=NOW)
    assert not ok.has_error and not ok.is_stale and ok.label.endswith("мин назад")

    bad = await sync.freshness(session, SYS, "velse", "warehouse", now=NOW)
    assert bad.has_error and bad.is_stale and "ошибка" in bad.label and "таймаут" in bad.message

    holding = await sync.freshness(session, SYS, "holding", "warehouse", now=NOW)
    assert holding.has_error and holding.last_success_at == bad.last_success_at  # худшая компания


async def test_freshness_respects_access(session, actor):
    import pytest

    from app.services.errors import PermissionDenied
    emp = await actor(SALES_FLER)
    await sync.freshness(session, emp, "fler", "sales")
    await sync.freshness(session, emp, "fler")  # сводка
    with pytest.raises(PermissionDenied):
        await sync.freshness(session, emp, "fler", "finance")


def test_humanize_age():
    now = datetime(2026, 11, 21, 12, 0)
    assert humanize_age(now - timedelta(seconds=20), now) == "только что"
    assert humanize_age(now - timedelta(minutes=5), now) == "5 мин назад"
    assert humanize_age(now - timedelta(hours=3), now) == "3 ч назад"
    assert humanize_age(now - timedelta(hours=26), now).startswith("вчера в")
    assert humanize_age(now - timedelta(days=5), now) == "5 дней назад"
    assert humanize_age(now - timedelta(days=2, hours=1), now) == "2 дня назад"
    assert humanize_age(None, now) == "нет данных"
