"""Цикл tool use поверх Messages API.

История каждого чата хранится в памяти и только дополняется (append-only):
так сохраняются блоки thinking и кеш. Чтобы ограничить размер, начинаем новый
разговор (/new или после простоя), а не обрезаем старый.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import tools as T
from app.services.changes import ChangePreview
from app.services.sandbox import Sandbox
from app.services.context import Actor
from app.services.format import fmt_date
from app.services.periods import today_local
from app.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_STEPS = 25  # разбор базы 1С занимает много шагов
IDLE_RESET = timedelta(hours=3)
MAX_INPUT_TOKENS = 300_000  # после такого разговора начинаем новый
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Серверные инструменты Anthropic: поиск и чтение страниц (документация API сервисов)
WEB_TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8}]


@dataclass
class Conversation:
    messages: list[dict] = field(default_factory=list)
    last_used: datetime = field(default_factory=utcnow)
    last_input_tokens: int = 0
    # Системный промпт закрепляется на весь разговор: блоки thinking привязаны к нему,
    # и смена промпта посреди разговора — ошибка 400. Изменения каталога идут сообщением.
    system: list[dict] | None = None
    catalog: str | None = None

    def reset(self) -> None:
        self.messages.clear()
        self.system = self.catalog = None

    def is_stale(self) -> bool:
        return utcnow() - self.last_used > IDLE_RESET or self.last_input_tokens > MAX_INPUT_TOKENS

    def add_note(self, text: str) -> None:
        """Системная пометка от бота (подтверждение/отмена). Добавляется как user-сообщение."""
        self.messages.append({"role": "user", "content": f"[бот] {text}"})


@dataclass
class TurnResult:
    text: str
    previews: list[ChangePreview] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        system_prompt: str,
        sessionmaker: async_sessionmaker,
        load_actor: Callable[[AsyncSession], Awaitable[Actor]],
        allow_plans: bool = True,
        effort: str = "medium",
        use_fallbacks: bool = True,
        catalog_builder: Callable[[AsyncSession], Awaitable[str]] | None = None,
        web_tools: bool = False,
    ):
        self.client = client
        self.model = model
        self.system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        self.sessionmaker = sessionmaker
        self.load_actor = load_actor
        self.allow_plans = allow_plans
        self.effort = effort
        self.use_fallbacks = use_fallbacks
        self.base_prompt = system_prompt
        self.web_tools = web_tools
        self.catalog_builder = catalog_builder  # каталог (компании, показатели) читается из БД

    async def _create(self, system: list[dict], tools_api: list[dict], messages: list[dict]):
        if self.web_tools:
            tools_api = tools_api + WEB_TOOLS
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            tools=tools_api,
            messages=messages,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.use_fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        return await self.client.beta.messages.create(**kwargs)

    async def run_turn(self, conv: Conversation, user_text: str, attachments: list[dict] | None = None) -> TurnResult:
        """attachments — блоки для Claude из присланных файлов (картинка, PDF, разобранная таблица)."""
        today = today_local()
        previews: list[ChangePreview] = []

        async with self.sessionmaker() as session, Sandbox(session) as sandbox:
            actor = await self.load_actor(session)  # каждый ход заново: права могли измениться
            prefix = ""
            if self.catalog_builder:
                catalog = await self.catalog_builder(session)
                if conv.system is None:
                    conv.catalog = catalog
                    conv.system = [{"type": "text", "text": f"{self.base_prompt}\n\n{catalog}",
                                    "cache_control": {"type": "ephemeral"}}]
                elif catalog != conv.catalog:
                    conv.catalog = catalog
                    prefix = f"[бот] Каталог изменился, актуальный вариант (заменяет тот, что в начале):\n{catalog}\n\n"
            system = conv.system or self.system
            text_block = f"{prefix}[сегодня {fmt_date(today)}] {user_text}"
            content = [*attachments, {"type": "text", "text": text_block}] if attachments else text_block
            conv.messages.append({"role": "user", "content": content})
            conv.last_used = utcnow()
            allowed = {t.name: t for t in T.tools_for(actor, self.allow_plans)}
            tools_api = [t.to_api() for t in allowed.values()]

            for _ in range(MAX_STEPS):
                resp = await self._create(system, tools_api, conv.messages)
                u = resp.usage
                conv.last_input_tokens = (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (
                    u.cache_creation_input_tokens or 0)
                log.info("claude %s stop=%s in=%s cache_read=%s out=%s", resp.model, resp.stop_reason,
                         u.input_tokens, u.cache_read_input_tokens, u.output_tokens)

                if resp.stop_reason == "refusal":
                    return TurnResult("Не могу ответить на этот запрос. Попробуйте сформулировать иначе.", previews)

                conv.messages.append({"role": "assistant", "content": resp.content})
                text = "\n".join(b.text for b in resp.content if b.type == "text").strip()

                if resp.stop_reason == "pause_turn":
                    continue
                if resp.stop_reason == "max_tokens":
                    conv.reset()  # обрезанный ответ мог оборвать вызов инструмента
                    return TurnResult((text + "\n\n" if text else "") + "Ответ получился слишком длинным и обрезан. "
                                      "Начинаю новый разговор — задайте вопрос короче или по частям.", previews)
                if resp.stop_reason != "tool_use":
                    return TurnResult(text or "Готово.", previews)

                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    log.info("tool %s %s", block.name, block.input)
                    out = await T.execute(session, actor, block.name, dict(block.input or {}), allowed, sandbox)
                    if out.preview:
                        previews.append(out.preview)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": out.content, "is_error": out.is_error})
                conv.messages.append({"role": "user", "content": results})

        return TurnResult("Запрос потребовал слишком много шагов, я остановился. Уточните, что именно нужно.", previews)
