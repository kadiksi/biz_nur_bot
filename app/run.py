"""Обычный запуск: сайт и главный бот в одном процессе.

    python -m app.run
"""
import asyncio
import contextlib
import logging

import uvicorn

from app.bots import main_bot
from app.config import settings
from app.db import get_sessionmaker
from app.models import Channel
from app.services import onec
from app.services.context import Actor
from app.web.app import app as web_app
from app.web.app import check_port_free, site_url

log = logging.getLogger("run")


async def onec_loop() -> None:
    """Подтягивает факты из 1С: раз в ONEC_SYNC_MINUTES, а новые источники — в течение минуты
    после подтверждения. Ошибки не роняют сайт и бота."""
    actor = Actor.system(Channel.SCHEDULER)
    interval = settings.onec_sync_minutes * 60
    last_full = None
    await asyncio.sleep(20)
    while True:
        now = asyncio.get_running_loop().time()
        due = interval > 0 and (last_full is None or now - last_full >= interval)
        try:
            async with get_sessionmaker()() as session:
                result = await onec.scheduled_load(session, actor, due=due)
            if result:
                log.info("1С: %s", result)
        except Exception:
            log.exception("1С: загрузка не удалась")
        if due:
            last_full = now
        wake = onec.wake_event()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=60)
        wake.clear()


async def main() -> None:
    main_bot.setup_logging()
    check_port_free()
    bot, dp = await main_bot.build()
    server = uvicorn.Server(uvicorn.Config(web_app, host=settings.site_host, port=settings.site_port,
                                           log_level="warning"))
    log.info("Сайт: %s (открывается из той же сети)", site_url())
    tasks = {
        asyncio.create_task(server.serve(), name="site"),
        asyncio.create_task(dp.start_polling(bot, handle_signals=False,
                                             allowed_updates=dp.resolve_used_update_types()), name="bot"),
        asyncio.create_task(onec_loop(), name="1c"),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t.exception():
            log.error("%s остановился с ошибкой", t.get_name(), exc_info=t.exception())
    with contextlib.suppress(RuntimeError):
        await dp.stop_polling()
    server.should_exit = True
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(t, timeout=5)
    await bot.session.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
