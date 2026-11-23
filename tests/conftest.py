from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import create_tables, make_engine
from app.models import Channel
from app.services.context import load_actor
from scripts.seed_demo import seed

OWNER_TG = 555000111
TODAY = date(2026, 11, 21)
NOW = datetime(2026, 11, 21, 9, 0)

# сотрудники из демо-данных
SALES_HEAD = 900000001       # ОП, все компании
SALES_FLER = 900000002       # ОП, только FLÉR
MARKETING_REMELY = 900000004  # маркетинг Remely + грант на чтение ОП FLÉR
WAREHOUSE_VELSE = 900000006
INACTIVE = 900000009


@pytest.fixture(autouse=True)
def _owner_setting(monkeypatch):
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_TG)


@pytest_asyncio.fixture
async def session():
    engine = make_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    await create_tables(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        assert await seed(s, OWNER_TG, today=TODAY, now=NOW)
        yield s
    await engine.dispose()


@pytest.fixture
def actor(session):
    async def _load(tg: int, channel: Channel = Channel.SITE):
        return await load_actor(session, tg, channel)

    return _load
