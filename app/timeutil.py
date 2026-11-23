"""Время: в БД храним naive UTC, показываем в settings.timezone."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def local_tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC).astimezone(local_tz())


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
