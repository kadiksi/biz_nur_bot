"""Вход на сайт по одноразовой ссылке из бота.

Бот выдаёт ссылку на 10 минут → браузер обменивает её на сессию на 30 дней.
В базе хранится только SHA-256 токенов, сам токен — только в ссылке и cookie.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Employee, WebToken
from app.services.context import Actor, actor_from_employee
from app.services.errors import PermissionDenied
from app.timeutil import utcnow

LOGIN_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=30)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_login_link_token(session: AsyncSession, telegram_id: int, now: datetime | None = None) -> str:
    """Для бота: одноразовый токен входа. Только для активных сотрудников."""
    now = now or utcnow()
    emp = await session.scalar(select(Employee).where(Employee.telegram_id == telegram_id))
    if emp is None or not emp.is_active:
        raise PermissionDenied("Нет доступа к сайту: аккаунт не зарегистрирован или отключён")
    await session.execute(delete(WebToken).where(WebToken.expires_at < now))
    token = secrets.token_urlsafe(24)
    session.add(WebToken(token_hash=_hash(token), kind="login", employee_id=emp.id, expires_at=now + LOGIN_TTL))
    await session.commit()
    return token


async def login_token_valid(session: AsyncSession, token: str, now: datetime | None = None) -> Employee | None:
    now = now or utcnow()
    row = await session.scalar(select(WebToken).where(WebToken.token_hash == _hash(token), WebToken.kind == "login"))
    if row is None or row.used_at is not None or row.expires_at < now:
        return None
    emp = await session.get(Employee, row.employee_id)
    return emp if emp and emp.is_active else None


async def exchange_login_token(session: AsyncSession, token: str, now: datetime | None = None) -> str | None:
    """Гасит ссылку и возвращает токен сессии (или None, если ссылка недействительна)."""
    now = now or utcnow()
    emp = await login_token_valid(session, token, now)
    if emp is None:
        return None
    row = await session.scalar(select(WebToken).where(WebToken.token_hash == _hash(token)))
    row.used_at = now
    sess = secrets.token_urlsafe(32)
    session.add(WebToken(token_hash=_hash(sess), kind="session", employee_id=emp.id, expires_at=now + SESSION_TTL))
    await session.commit()
    return sess


async def actor_for_session(session: AsyncSession, token: str | None, now: datetime | None = None) -> Actor | None:
    """None — не вошёл, сессия истекла или сотрудник отключён."""
    if not token:
        return None
    now = now or utcnow()
    row = await session.scalar(select(WebToken).where(WebToken.token_hash == _hash(token), WebToken.kind == "session"))
    if row is None or row.expires_at < now:
        return None
    emp = await session.get(Employee, row.employee_id)
    if emp is None or not emp.is_active:
        return None
    return await actor_from_employee(session, emp, Channel.SITE)


async def logout(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(delete(WebToken).where(WebToken.token_hash == _hash(token)))
        await session.commit()
