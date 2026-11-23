"""Песочница для цепочки изменений: копия базы в памяти.

Бот за один ход часто готовит связанные изменения: компанию, отдел, показатели этого
отдела, источники 1С для них и страницу. По отдельности показатель не пройдёт проверку,
пока отдела нет. Поэтому каждое изменение хода сначала проверяется (preview) и тут же
применяется к копии базы, и следующее уже видит предыдущие. Настоящая база не меняется
до «Подтвердить».

Операции с внешними последствиями (запрос в сервис, запись в таблицу, загрузка из 1С)
в копии только проверяются и не выполняются.
"""
from __future__ import annotations

import logging

import aiosqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import StaticPool

from app.services import changes
from app.services.changes import ChangePreview
from app.services.context import Actor
from app.services.errors import ServiceError

log = logging.getLogger(__name__)


class Sandbox:
    def __init__(self, source: AsyncSession):
        self.source = source
        self.session: AsyncSession | None = None
        self._engine = None
        self._target = None

    async def __aenter__(self) -> Sandbox:
        from app.db import make_engine

        bind = self.source.bind
        if bind is None or bind.dialect.name != "sqlite":
            return self  # не SQLite — проверяем по настоящей базе, как раньше
        try:
            conn = await self.source.connection()
            raw = await conn.get_raw_connection()
            self._target = await aiosqlite.connect(":memory:")
            await raw.driver_connection.backup(self._target)
            await self.source.commit()  # отпустить чтение (в источнике ничего не менялось)

            async def creator():
                return self._target

            self._engine = make_engine("sqlite+aiosqlite://", async_creator=creator, poolclass=StaticPool)
            self.session = AsyncSession(self._engine, expire_on_commit=False)
        except Exception:  # noqa: BLE001 — без песочницы бот всё равно работает, просто без цепочек
            log.exception("sandbox: не удалось скопировать базу")
            await self._close()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._close()

    async def _close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        if self._target is not None:
            try:
                await self._target.close()
            except Exception:  # noqa: BLE001
                pass
            self._target = None

    async def step(self, actor: Actor, op: str, params: dict) -> ChangePreview:
        """Проверить изменение с учётом предыдущих в этой песочнице и применить его к копии."""
        if self.session is None:
            return await changes.preview(self.source, actor, op, params)
        p = await changes.preview(self.session, actor, op, params)
        if not changes.is_external(op):
            try:
                await changes.apply(self.session, actor, op, params)
            except ServiceError:
                await self.session.rollback()
                raise
        return p
