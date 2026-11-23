"""Главный бот без сети: инструменты, подтверждение, цикл с поддельным клиентом Claude."""
import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent import tools as T
from app.agent.prompts import main_system_prompt
from app.agent.runner import MAX_STEPS, Agent, Conversation
from app.bots import pending
from app.bots.main_bot import chunks
from app.models import ChangeLog, Channel
from app.services import changes
from app.services.context import load_actor
from tests.conftest import OWNER_TG, SALES_FLER


async def _owner(session):
    return await load_actor(session, OWNER_TG, Channel.MAIN_BOT)


def _allowed(actor, allow_plans=True):
    return {t.name: t for t in T.tools_for(actor, allow_plans)}


async def _log_count(session):
    return await session.scalar(select(func.count()).select_from(ChangeLog))


# ---------- инструменты ----------

READ_CALLS = [
    ("get_summary", {"company": "holding", "period": "month"}),
    ("get_department_metrics", {"company": "fler", "department": "sales", "period": "half"}),
    ("get_metric", {"company": "velse", "metric": "cpl", "period": "2026-09..2026-11"}),
    ("get_freshness", {"company": "velse", "department": "warehouse"}),
    ("get_kanban", {"company": "holding", "department": "marketing"}),
    ("list_employees", {"include_inactive": True}),
    ("list_recurring_tasks", {}),
    ("get_change_log", {"limit": 5}),
]


@pytest.mark.parametrize("name,args", READ_CALLS)
async def test_read_tools_return_json(session, name, args):
    owner = await _owner(session)
    out = await T.execute(session, owner, name, args, _allowed(owner))
    assert not out.is_error, out.content
    assert json.loads(out.content)


async def test_metric_tool_has_months_and_freshness(session):
    owner = await _owner(session)
    out = json.loads((await T.execute(session, owner, "get_department_metrics",
                                      {"company": "fler", "department": "sales", "period": "half"}, _allowed(owner))).content)
    revenue = next(m for m in out["metrics"] if m["code"] == "revenue")
    assert len(revenue["by_month"]) == 6 and "обновлено" in out["freshness"]


async def test_write_tool_only_previews(session):
    owner = await _owner(session)
    n = await _log_count(session)
    out = await T.execute(session, owner, "set_plan",
                          {"company": "fler", "metric": "revenue", "month": "2026-12", "value": 32_000_000},
                          _allowed(owner))
    assert out.preview and not out.preview.applied and "НЕ применено" in out.content
    assert await _log_count(session) == n


async def test_errors_come_back_as_tool_errors(session):
    owner = await _owner(session)
    for name, args in [("set_plan", {"company": "fler", "metric": "avg_check", "month": "2026-12", "value": 1}),
                       ("get_metric", {"company": "nope", "metric": "revenue", "period": "month"}),
                       ("get_summary", {"company": "fler"}),  # нет period
                       ("unknown_tool", {})]:
        out = await T.execute(session, owner, name, args, _allowed(owner))
        assert out.is_error and out.content.startswith(("Ошибка", "Инструмент"))


async def test_employee_does_not_get_owner_tools(session):
    emp = await load_actor(session, SALES_FLER, Channel.DEPT_BOT)
    names = set(_allowed(emp, allow_plans=False))
    assert {"get_kanban", "create_card", "move_card"} <= names
    assert not names & {"set_plan", "create_employee", "grant_access", "get_change_log", "create_recurring_task"}


def test_tool_schemas_are_valid_for_api():
    for t in T.ALL_TOOLS.values():
        api = t.to_api()
        assert api["input_schema"]["type"] == "object"
        assert set(api["input_schema"]["required"]) <= set(api["input_schema"]["properties"])


async def test_system_prompt_contains_catalog(session):
    text = await main_system_prompt(session)
    assert "revenue — Выручка" in text and "формула: revenue / orders" in text and "FLÉR" in text


# ---------- подтверждение ----------


async def _preview(session, actor, value=32_000_000):
    return await changes.preview(session, actor, "plans.set_plan",
                                 {"company": "fler", "metric": "revenue", "month": "2026-12", "value": value})


async def test_confirm_applies_batch(session):
    owner = await _owner(session)
    store = pending.PendingStore()
    from app.services.sandbox import Sandbox

    params = {"company": "fler", "metric": "revenue", "month": "2026-12"}
    async with Sandbox(session) as sb:  # как в ходе бота: второе изменение видит первое
        previews = [await sb.step(owner, "plans.set_plan", {**params, "value": v}) for v in (32_000_000, 5)]
    assert "32" in previews[1].render()  # «было» во втором — значение из первого
    batch = store.add(1, previews)
    assert store.pop(batch.id, chat_id=2) is None  # чужой чат
    batch = store.add(1, batch.previews)
    n = await _log_count(session)
    res = await pending.confirm(session, owner, store.pop(batch.id, 1))
    assert len(res.applied) == 2 and not res.errors and res.changed is None
    assert await _log_count(session) == n + 2
    assert store.pop(batch.id, 1) is None  # второй раз не применить


async def test_confirm_detects_changed_data(session):
    owner = await _owner(session)
    batch = pending.PendingStore().add(1, [await _preview(session, owner)])
    await changes.apply(session, owner, "plans.set_plan",
                        {"company": "fler", "metric": "revenue", "month": "2026-12", "value": 1})
    n = await _log_count(session)
    res = await pending.confirm(session, owner, batch)
    assert res.changed and not res.applied and await _log_count(session) == n


def test_pending_expires():
    store = pending.PendingStore()
    batch = store.add(1, [])
    batch.created_at -= pending.TTL + timedelta(seconds=1)
    assert store.pop(batch.id, 1) is None


def test_chunks_split_long_text():
    parts = chunks("строка\n" * 2000)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)


# ---------- цикл агента ----------


def _usage():
    return NS(input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=10)


def _resp(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks), usage=_usage(), model="claude-opus-5-5")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def _agent(session, client):
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    return Agent(client, "claude-opus-5-5", "SYSTEM", maker, _owner)


async def test_agent_reads_then_answers(session):
    client = FakeClient([
        _resp("tool_use", NS(type="text", text="Смотрю."),
              NS(type="tool_use", id="t1", name="get_metric",
                 input={"company": "holding", "metric": "revenue", "period": "month"})),
        _resp("end_turn", NS(type="text", text="Выручка холдинга — 30,1 млн ₽.")),
    ])
    conv = Conversation()
    res = await _agent(session, client).run_turn(conv, "Какая выручка?")
    assert res.text == "Выручка холдинга — 30,1 млн ₽." and not res.previews
    roles = [m["role"] for m in conv.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_result = conv.messages[2]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and not tool_result["is_error"]
    assert json.loads(tool_result["content"])["code"] == "revenue"

    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium"} and "tool_choice" not in kw and "thinking" not in kw
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in kw["tools"]} == set(T.ALL_TOOLS)
    # история только дополняется: второй запрос начинается с первого
    assert client.calls[1]["messages"][:1] == client.calls[0]["messages"]


async def test_agent_collects_previews_without_writing(session):
    n = await _log_count(session)
    client = FakeClient([
        _resp("tool_use",
              NS(type="tool_use", id="a", name="set_plan",
                 input={"company": "velse", "metric": "revenue", "month": "2026-12", "value": 9_000_000}),
              NS(type="tool_use", id="b", name="create_card",
                 input={"company": "remely", "department": "marketing", "title": "Рассылка"})),
        _resp("end_turn", NS(type="text", text="Подготовил два изменения, подтвердите.")),
    ])
    res = await _agent(session, client).run_turn(Conversation(), "план и карточка")
    assert [p.op for p in res.previews] == ["plans.set_plan", "kanban.create_card"]
    assert await _log_count(session) == n
    # оба результата — в одном user-сообщении
    assert len(client.calls[1]["messages"][-1]["content"]) == 2


async def test_agent_handles_refusal_and_step_limit(session):
    res = await _agent(session, FakeClient([_resp("refusal")])).run_turn(Conversation(), "?")
    assert "Не могу" in res.text

    loop = [_resp("tool_use", NS(type="tool_use", id=f"t{i}", name="get_freshness", input={"company": "fler"}))
            for i in range(MAX_STEPS)]
    res = await _agent(session, FakeClient(loop)).run_turn(Conversation(), "?")
    assert "слишком много шагов" in res.text


def test_conversation_resets_when_idle_or_large():
    conv = Conversation()
    assert not conv.is_stale()
    conv.last_used -= timedelta(hours=4)
    assert conv.is_stale()
    conv = Conversation(last_input_tokens=400_000)
    assert conv.is_stale()


async def test_system_prompt_pinned_when_catalog_changes(session):
    """Блоки thinking привязаны к system — менять его посреди разговора нельзя (ошибка 400)."""
    from app.agent.prompts import build_catalog
    from app.services import changes

    client = FakeClient([_resp("end_turn", NS(type="text", text="Ок.")), _resp("end_turn", NS(type="text", text="Ок."))])
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    agent = Agent(client, "claude-opus-5-5", "РОЛЬ", maker, _owner, catalog_builder=build_catalog)
    conv = Conversation()
    await agent.run_turn(conv, "привет")
    async with maker() as s:
        await changes.apply(s, await _owner(s), "companies.create", {"code": "alpha", "name": "Альфа"})
    await agent.run_turn(conv, "что нового?")
    first, second = client.calls
    assert first["system"] == second["system"] and "alpha" not in first["system"][0]["text"]
    last = second["messages"][-1]["content"]
    assert last.startswith("[бот] Каталог изменился") and "alpha — Альфа" in last and last.endswith("что нового?")
    assert "[бот] Каталог" not in first["messages"][-1]["content"]
