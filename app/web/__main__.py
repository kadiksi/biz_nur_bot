"""Только сайт: python -m app.web"""
import asyncio

import uvicorn

from app.config import settings
from app.db import create_tables, get_engine
from app.web.app import app, check_port_free, site_url

async def _prepare() -> None:
    await create_tables(get_engine())
    await get_engine().dispose()  # соединения этого цикла событий не переносим в uvicorn


if __name__ == "__main__":
    check_port_free()
    asyncio.run(_prepare())
    print(f"Сайт: {site_url()}  (вход — команда /site в главном боте)")
    uvicorn.run(app, host=settings.site_host, port=settings.site_port, log_level="warning")
