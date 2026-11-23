"""Главный бот: только для OWNER_TELEGRAM_ID.

    python -m app.run              # бот + сайт (обычный запуск)
    python -m app.bots.main_bot    # только бот
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.chat_action import ChatActionSender

from app.agent import uploads
from app.agent.prompts import MAIN_ROLE, build_catalog
from app.agent.runner import Agent, Conversation
from app.bots import pending
from app.config import settings
from app.db import create_tables, get_engine, get_sessionmaker
from app.integrations import speech
from app.models import Channel
from app.services.context import load_actor
from app.services import onec, secrets, webauth
from app.services.errors import PermissionDenied, ServiceError
from app.web.app import site_url

log = logging.getLogger("main_bot")
TG_LIMIT = 4000

router = Router()
conversations: dict[int, Conversation] = {}
locks: dict[int, asyncio.Lock] = {}
store = pending.PendingStore()
agent: Agent | None = None


def is_owner(user_id: int | None) -> bool:
    return settings.owner_telegram_id is not None and user_id == settings.owner_telegram_id


def chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out or ["…"]


def confirm_kb(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"cf:{batch_id}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"cx:{batch_id}"),
    ]])


def conversation(chat_id: int) -> Conversation:
    conv = conversations.get(chat_id)
    if conv is None or conv.is_stale():
        conv = conversations[chat_id] = Conversation()
    return conv


async def owner_actor(session):
    actor = await load_actor(session, settings.owner_telegram_id, Channel.MAIN_BOT)
    if not actor.is_owner:
        raise PermissionDenied("В базе у OWNER_TELEGRAM_ID нет роли владельца")
    return actor


async def send_previews(message: Message, previews) -> None:
    batch = store.add(message.chat.id, previews)
    text = pending.render_batch(previews)
    parts = chunks(text)
    for part in parts[:-1]:
        await message.answer(part)
    await message.answer(parts[-1], reply_markup=confirm_kb(batch.id))


# ---------- доступ ----------


@router.message(~F.from_user.func(lambda u: is_owner(u.id)))
async def not_owner(message: Message) -> None:
    await message.answer("Этот бот доступен только владельцу.")


@router.callback_query(~F.from_user.func(lambda u: is_owner(u.id)))
async def not_owner_cb(cb: CallbackQuery) -> None:
    await cb.answer("Нет доступа", show_alert=True)


# ---------- команды ----------


@router.message(CommandStart())
async def start(message: Message) -> None:
    conversations.pop(message.chat.id, None)
    await message.answer(
        "Здравствуйте! Спрашивайте о цифрах холдинга и давайте поручения обычным текстом. Например:\n"
        "• Как ОП выполняет план в этом месяце?\n"
        "• Добавь компанию …\n"
        "• Подключи выручку из 1С\n"
        "• Поставь план выручки на декабрь 9 млн\n"
        "• Создай карточку маркетингу «Запустить рассылку» до 1 декабря\n"
        "• Пришлите Excel, CSV, PDF или фото с подписью, что с ним сделать\n"
        "• Говорите голосом или пришлите запись встречи — расшифрую\n\n"
        "Любое изменение я сначала покажу и применю только после кнопки «Подтвердить».\n"
        "Сайт со страницами, которые я собираю, — команда /site.\n"
        "/new — начать новый разговор."
    )


@router.message(Command("site"))
async def site_link(message: Message) -> None:
    async with get_sessionmaker()() as session:
        token = await webauth.create_login_link_token(session, message.from_user.id)
    await message.answer(
        f"Ссылка для входа на сайт (действует 10 минут, один раз):\n{site_url()}/login/{token}\n\n"
        "Открывайте с устройства в той же Wi‑Fi сети, что и компьютер с ботом. Вход запомнится на 30 дней.",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(Command("new"))
async def new_conversation(message: Message) -> None:
    conversations.pop(message.chat.id, None)
    await message.answer("Начинаем новый разговор.")


# ---------- сообщения ----------


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    await handle(message, bot, message.text)


FILE_DEFAULT = "Посмотри присланный файл: коротко скажи, что в нём, и предложи, что из этого сделать на сайте."


AUDIO_DEFAULT = ("Это расшифровка присланной записи. Коротко перескажи: о чём речь, какие решения, поручения, сроки и "
                 "числа прозвучали. Ничего не меняй, предложи, что из этого можно сделать.")
TRANSCRIPT_INLINE = 3500  # длиннее — отправляем файлом


def audio_of(message: Message):
    """(файл Telegram, имя, это личное голосовое?) или None."""
    if message.voice:
        return message.voice, "voice.ogg", True
    if message.video_note:
        return message.video_note, "video_note.mp4", True
    if message.audio:
        return message.audio, message.audio.file_name or "audio.mp3", False
    if message.video:
        return message.video, message.video.file_name or "video.mp4", False
    if message.document and speech.is_audio(message.document.file_name, message.document.mime_type):
        return message.document, message.document.file_name or "audio", False
    return None


@router.message(audio_of)
async def on_audio(message: Message, bot: Bot) -> None:
    tg_file, name, own_voice = audio_of(message)
    # пересланное голосовое — чужая речь: материал, а не поручение владельца
    is_order = own_voice and message.forward_origin is None
    if (tg_file.file_size or 0) > speech.MAX_BYTES:
        await message.answer("Telegram не отдаёт ботам файлы больше 20 МБ. Пришлите запись по частям или сожмите её.")
        return
    first = "" if speech.models_ready() else " Первый раз скачиваю модель распознавания (≈ 550 МБ), это займёт пару минут."
    status = await message.answer("🎙 Расшифровываю…" + first)
    shown = 0.0

    async def on_progress(p: float) -> None:
        nonlocal shown
        now = asyncio.get_running_loop().time()
        if now - shown >= 3:  # не чаще: Telegram ограничивает правки
            shown = now
            with contextlib.suppress(TelegramAPIError):
                await status.edit_text(f"🎙 Расшифровываю… {round(p * 100)} %")

    try:
        buf = await bot.download(tg_file)
        tr = await speech.transcribe(buf.read(), name, on_progress)
    except TelegramAPIError:
        log.exception("audio download failed")
        await status.edit_text("Не получилось скачать запись из Telegram. Пришлите ещё раз.")
        return
    except ServiceError as e:
        await status.edit_text(str(e))
        return
    except Exception:
        log.exception("transcription failed")
        await status.edit_text("Не получилось расшифровать, подробности в логе.")
        return

    caption = (message.caption or "").strip()
    body = tr.text if is_order else tr.timed()
    if len(body) <= TRANSCRIPT_INLINE:
        await status.edit_text(f"🎙 Расшифровка ({speech.fmt_time(tr.duration)}):\n\n{body}")
    else:
        await status.edit_text(f"🎙 Расшифровал, {speech.fmt_time(tr.duration)}. Полный текст — в файле.")
        await message.answer_document(BufferedInputFile(body.encode("utf-8-sig"), "расшифровка.txt"))
    if is_order:
        await handle(message, bot, f"(голосовое) {tr.text}" + (f"\n{caption}" if caption else ""))
        return
    _, blocks = uploads.add_text(f"Расшифровка: {name}", body)
    await handle(message, bot, f"(прислана запись «{name}», {speech.fmt_time(tr.duration)}) {caption or AUDIO_DEFAULT}", blocks)


@router.message(F.document | F.photo)
async def on_file(message: Message, bot: Bot) -> None:
    if message.photo:
        tg_file, name = message.photo[-1], "photo.jpg"  # самое большое разрешение
    else:
        tg_file, name = message.document, message.document.file_name or "file"
    if (tg_file.file_size or 0) > uploads.MAX_BYTES:
        await message.answer(f"Файл слишком большой. Пришлите до {uploads.MAX_BYTES // 1024 // 1024} МБ.")
        return
    try:
        buf = await bot.download(tg_file)
        _, blocks = uploads.add(name, buf.read())
    except TelegramBadRequest:
        log.exception("download failed")
        await message.answer("Не получилось скачать файл из Telegram (до 20 МБ). Пришлите ещё раз.")
        return
    except ServiceError as e:
        await message.answer(str(e))
        return
    text = (message.caption or "").strip() or FILE_DEFAULT
    await handle(message, bot, f"(прислан файл «{name}») {text}", blocks)


async def handle(message: Message, bot: Bot, text: str, attachments: list[dict] | None = None) -> None:
    chat_id = message.chat.id
    lock = locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        conv = conversation(chat_id)
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                result = await agent.run_turn(conv, text, attachments)
        except anthropic.RateLimitError:
            await message.answer("Claude сейчас перегружен запросами. Попробуйте через минуту.")
            return
        except anthropic.APIConnectionError:
            await message.answer("Нет связи с Claude. Попробуйте ещё раз.")
            return
        except anthropic.BadRequestError:
            # запрос отклонён из-за самого разговора — повтор упадёт так же, начинаем заново
            log.exception("Claude API error")
            conversations.pop(chat_id, None)
            await message.answer("Claude отклонил запрос (ошибка 400), подробности в логе. "
                                 "Начинаю новый разговор — повторите, пожалуйста, последнюю просьбу.")
            return
        except anthropic.APIStatusError as e:
            log.exception("Claude API error")
            await message.answer(f"Ошибка Claude API ({e.status_code}). Подробности в логе бота.")
            return
        except PermissionDenied as e:
            await message.answer(str(e))
            return
        except Exception:
            log.exception("turn failed")
            conversations.pop(chat_id, None)
            await message.answer("Что-то пошло не так, подробности в логе. Начинаю новый разговор.")
            return

        for part in chunks(result.text):
            await message.answer(part)
        if result.previews:
            await send_previews(message, result.previews)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(f"Такое я не читаю. Понимаю текст, голосовые и аудио, файлы: {uploads.SUPPORTED}.")


# ---------- подтверждение ----------


@router.callback_query(F.data.startswith("cx:"))
async def on_cancel(cb: CallbackQuery) -> None:
    batch = store.pop(cb.data[3:], cb.message.chat.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    if batch is None:
        await cb.answer("Уже неактуально")
        return
    conversation(cb.message.chat.id).add_note(
        "Владелец нажал «Отмена». Не применено:\n" + pending.render_batch(batch.previews))
    await cb.message.answer("Отменено, ничего не изменилось.")
    await cb.answer()


@router.callback_query(F.data.startswith("cf:"))
async def on_confirm(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    batch = store.pop(cb.data[3:], chat_id)
    await cb.message.edit_reply_markup(reply_markup=None)
    if batch is None:
        await cb.answer("Предпросмотр устарел — попросите подготовить изменение заново", show_alert=True)
        return
    await cb.answer("Применяю…")
    ops = {p.op for p in batch.previews}
    watcher = None
    if ops & {"onec.load_facts", "onec.save_source"}:
        watcher = asyncio.create_task(watch_progress(cb.message, onec.PROGRESS.run_id))
    async with locks.setdefault(chat_id, asyncio.Lock()):
        async with get_sessionmaker()() as session:
            actor = await owner_actor(session)
            res = await pending.confirm(session, actor, batch)
        conv = conversation(chat_id)
        if res.changed is not None:
            conv.add_note("Данные изменились до подтверждения, показан обновлённый предпросмотр.")
            await cb.message.answer("Пока вы смотрели, данные изменились. Проверьте ещё раз:")
            await send_previews(cb.message, res.changed)
            return
        if watcher and (res.changed is not None or not res.applied):
            watcher.cancel()
        text = ("✅ " if not res.errors else "⚠️ ") + res.summary()
        links = page_links(res.applied)
        if links:
            text += "\n\n" + "\n".join(links) + "\n(если сайт просит войти — команда /site)"
        await cb.message.answer(text, link_preview_options=LinkPreviewOptions(is_disabled=True))
    # бот сам делает следующие шаги, если обещал их (или чинит то, что не применилось)
    await continue_turn(cb.bot, chat_id, CONFIRMED_NOTE.format(summary=res.summary()), quiet=True)


CONFIRMED_NOTE = ("[бот] Владелец нажал «Подтвердить». {summary}\nЕсли по задаче остались следующие шаги "
                  "(ты их обещал, или что-то не применилось и это можно исправить) — сразу выполни их: подготовь "
                  "изменения или продолжи настройку. Если продолжать нечего, ответь ровно «—».")
NOTHING = {"—", "-", "–", ""}


async def continue_turn(bot: Bot, chat_id: int, note: str, *, quiet: bool = False,
                        fail_prefix: str = "Продолжить не получилось") -> None:
    """Ход бота без сообщения владельца: после подтверждения или когда на сайте сохранили ключ.
    quiet — если бот ответил «—» (делать нечего), ничего не отправлять."""
    async with locks.setdefault(chat_id, asyncio.Lock()):
        conv = conversation(chat_id)
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                result = await agent.run_turn(conv, note)
        except anthropic.BadRequestError:
            log.exception("Claude API error")
            conversations.pop(chat_id, None)
            await bot.send_message(chat_id, f"{fail_prefix} (ошибка Claude). Напишите, что сделать дальше.")
            return
        except Exception:
            log.exception("continue turn failed")
            await bot.send_message(chat_id, f"{fail_prefix} — подробности в логе. Напишите, что сделать дальше.")
            return
        if not (quiet and result.text.strip() in NOTHING and not result.previews):
            for part in chunks(result.text):
                await bot.send_message(chat_id, part, link_preview_options=LinkPreviewOptions(is_disabled=True))
        if result.previews:
            batch = store.add(chat_id, result.previews)
            parts = chunks(pending.render_batch(result.previews))
            for part in parts[:-1]:
                await bot.send_message(chat_id, part)
            await bot.send_message(chat_id, parts[-1], reply_markup=confirm_kb(batch.id))


async def continue_after_secret(bot: Bot, key: str, title: str) -> None:
    """Владелец сохранил ключ на сайте — бот сам продолжает задачу, ради которой его просил."""
    chat_id = settings.owner_telegram_id
    await bot.send_message(chat_id, f"🔑 Получил: {title}. Продолжаю…")
    if key.startswith("ONEC_"):
        note = (f"[бот] Владелец добавил через сайт {title}, связь проверена. Подключение "
                f"«{key.removeprefix('ONEC_').lower()}» уже в onec_connections. Продолжай задачу.")
    else:
        note = (f"[бот] Владелец сохранил через сайт: «{title}» (в .env как {key}; значение тебе не "
                "показывается, инструменты подставят его сами). Продолжай задачу, ради которой просил ключ.")
    await continue_turn(bot, chat_id, note, fail_prefix="Ключ сохранён, но продолжить не получилось")


def progress_text(p: dict) -> str:
    filled = round(p["percent"] / 10)
    lines = [f"⏳ Загрузка из 1С · {p['period']}", f"{'▓' * filled}{'░' * (10 - filled)} {p['percent']} % "
             f"({p['done']} из {p['total']})"]
    if p.get("current"):
        lines.append(p["current"])
    return "\n".join(lines)


async def watch_progress(message: Message, after_run: int, wait_start: float = 120) -> None:
    """Сообщение с ходом загрузки из 1С, обновляется на месте до конца загрузки."""
    msg = await message.answer("⏳ Загрузка из 1С начинается…")
    loop = asyncio.get_running_loop()
    started, last = loop.time(), msg.text

    async def show(text: str) -> None:
        nonlocal last
        if text != last:
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(text, link_preview_options=LinkPreviewOptions(is_disabled=True))
            last = text

    try:
        while True:
            p = onec.progress()
            if p["run_id"] > after_run and not p["running"]:
                head = "✅ Загрузка из 1С завершена" if not p["errors"] else "⚠️ Загрузка из 1С завершена с ошибками"
                await show(f"{head}\n{p['result'] or ''}\nНа сайте: {site_url()}")
                return
            if p["run_id"] > after_run:
                await show(progress_text(p))
            elif loop.time() - started > wait_start:
                await show("Загрузка из 1С пока не началась — она пройдёт при следующем плановом обновлении.")
                return
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await msg.delete()
        raise


def page_links(applied) -> list[str]:
    slugs = [p.params["slug"] for p in applied if p.op in ("site.save_page", "site.update_page", "site.restore_page")]
    return [f"Открыть: {site_url()}/p/{s}" for s in dict.fromkeys(slugs)]


# ---------- запуск ----------


def check_settings() -> list[str]:
    missing = []
    if not settings.bot_token_main:
        missing.append("BOT_TOKEN_MAIN")
    if not settings.owner_telegram_id:
        missing.append("OWNER_TELEGRAM_ID")
    return missing


async def build() -> tuple[Bot, Dispatcher]:
    global agent
    if missing := check_settings():
        raise SystemExit(f"Заполните в .env: {', '.join(missing)}")
    await create_tables(get_engine())  # новые таблицы (страницы сайта) появятся сами
    maker = get_sessionmaker()
    async with maker() as session:
        await owner_actor(session)  # владелец есть в базе

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key \
        else anthropic.AsyncAnthropic()
    agent = Agent(client, settings.model_main, MAIN_ROLE, maker, owner_actor, catalog_builder=build_catalog,
                  web_tools=settings.claude_web_tools,
                  allow_plans=True, effort=settings.main_effort, use_fallbacks=settings.claude_fallbacks)
    bot = Bot(settings.bot_token_main)
    secrets.on_fulfilled(lambda key, title: continue_after_secret(bot, key, title))
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    log.info("Главный бот @%s запущен, модель %s", me.username, settings.model_main)
    return bot, dp


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    bot, dp = await build()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
