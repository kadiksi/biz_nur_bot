"""Сайт, который собирает бот: страницы, вход по ссылке, показ блоков, канбан."""
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.agent import tools as T
from app.bots.main_bot import page_links
from app.models import CardStatus, ChangeLog, Channel, KanbanCard, SitePage
from app.services import changes, kanban, site, webauth
from app.services.errors import NotFound, PermissionDenied, ValidationError
from app.web import app as web
from app.web.blocks import fmt_compact
from tests.conftest import INACTIVE, MARKETING_REMELY, NOW, OWNER_TG, SALES_FLER

SALES_BLOCKS = [
    {"type": "kpis", "metrics": ["revenue", "orders", "avg_check"]},
    {"type": "chart", "metric": "revenue", "width": "half"},
    {"type": "chart", "metric": "revenue", "compare_companies": True, "kind": "line", "width": "half"},
    {"type": "table", "department": "sales"},
    {"type": "kanban", "department": "sales"},
]
SUMMARY_BLOCKS = [{"type": "heading", "text": "Главное"}, {"type": "summary"}]


async def _owner(actor):
    return await actor(OWNER_TG, Channel.MAIN_BOT)


async def _save(session, owner, slug="sales", title="ОП", blocks=SALES_BLOCKS, **kw):
    return await changes.apply(session, owner, "site.save_page", {"slug": slug, "title": title, "blocks": blocks, **kw})


async def _logs(session, entity=None):
    q = select(func.count()).select_from(ChangeLog)
    if entity:
        q = q.where(ChangeLog.entity == entity)
    return await session.scalar(q)


# ---------- страницы в services ----------


@pytest.mark.parametrize("blocks,err", [
    ([], "хотя бы один"),
    ([{"type": "video"}], "type должен"),
    ([{"type": "kpis", "metrics": ["nope"]}], "неизвестный показатель"),
    ([{"type": "kpis", "metrics": ["deals_from_leads"]}], "служебный"),
    ([{"type": "table", "department": "sales", "metrics": ["revenue"]}], "либо department"),
    ([{"type": "chart", "metric": "revenue", "kind": "pie"}], "bar или line"),
    ([{"type": "kanban"}], "нужен department"),
    ([{"type": "text", "text": "x", "metrics": ["revenue"]}], "лишние поля"),
    ([{"type": "heading", "text": "x", "width": "huge"}], "width"),
])
async def test_block_validation(session, actor, blocks, err):
    with pytest.raises(ValidationError, match=err):
        await changes.preview(session, await _owner(actor), "site.save_page",
                              {"slug": "x", "title": "X", "blocks": blocks})


async def test_bad_slug(session, actor):
    with pytest.raises(ValidationError, match="латиница"):
        await _save(session, await _owner(actor), slug="Продажи")


async def test_preview_does_not_write_and_describes_blocks(session, actor):
    owner = await _owner(actor)
    n = await _logs(session)
    p = await changes.preview(session, owner, "site.save_page", {"slug": "sales", "title": "ОП", "blocks": SALES_BLOCKS})
    text = p.render()
    assert "Новая страница сайта «ОП»" in text and "/p/sales" in text
    assert "Карточки KPI · Выручка, Заказы, Средний чек" in text and "по компаниям" in text and "Канбан · ОП" in text
    assert await _logs(session) == n and await session.scalar(select(func.count()).select_from(SitePage)) == 0


async def test_save_update_restore_delete(session, actor):
    owner = await _owner(actor)
    await _save(session, owner, department="sales")
    page = await site.get_page(session, owner, "sales")
    assert page.version == 1 and page.blocks[0]["width"] == "full" and page.department == "sales"

    with pytest.raises(ValidationError, match="менять нечего"):
        await _save(session, owner, department="sales")

    await _save(session, owner, blocks=SALES_BLOCKS[:2], department="sales")
    page = await site.get_page(session, owner, "sales")
    assert page.version == 2 and len(page.blocks) == 2
    assert [v.version for v in await site.list_versions(session, owner, "sales")] == [1]

    p = await changes.apply(session, owner, "site.restore_page", {"slug": "sales", "version": 1})
    assert p.op == "site.restore_page" and "версии 1" in p.title
    assert len((await site.get_page(session, owner, "sales")).blocks) == 5

    await changes.apply(session, owner, "site.update_page", {"slug": "sales", "title": "Продажи", "sort_order": 5})
    assert (await site.get_page(session, owner, "sales")).title == "Продажи"

    await changes.apply(session, owner, "site.delete_page", {"slug": "sales"})
    with pytest.raises(NotFound):
        await site.get_page(session, owner, "sales")
    last = max(v.version for v in await site.list_versions(session, owner, "sales"))
    await changes.apply(session, owner, "site.restore_page", {"slug": "sales", "version": last})
    page = await site.get_page(session, owner, "sales")
    assert page.title == "Продажи" and page.version == last + 1
    # создание, замена, откат, переименование, удаление, возврат
    assert await _logs(session, "site_page") == 6


async def test_only_owner_changes_site(session, actor):
    emp = await actor(SALES_FLER, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await changes.preview(session, emp, "site.save_page", {"slug": "x", "title": "X", "blocks": SUMMARY_BLOCKS})


async def test_page_visibility_by_department(session, actor):
    owner = await _owner(actor)
    await _save(session, owner, department="sales")
    await _save(session, owner, slug="summary", title="Сводка", blocks=SUMMARY_BLOCKS)
    sales_emp, mkt_emp = await actor(SALES_FLER), await actor(MARKETING_REMELY)
    assert [p.slug for p in await site.list_pages(session, sales_emp)] == ["sales", "summary"]
    # у маркетолога Remely есть грант на ОП FLÉR — страницу ОП он тоже видит
    assert {p.slug for p in await site.list_pages(session, mkt_emp)} == {"sales", "summary"}
    wh = await actor(900000006)
    assert [p.slug for p in await site.list_pages(session, wh)] == ["summary"]
    with pytest.raises(NotFound):
        await site.get_page(session, wh, "sales")


def test_fmt_compact():
    assert fmt_compact(24_300_000, "₽") == "24,3 млн ₽"
    assert fmt_compact(45_600, "₽") == "46 тыс ₽"
    assert fmt_compact(12.5, "%", 1) == "12,5 %"
    assert fmt_compact(None, "₽") == "—"


# ---------- вход ----------


async def test_login_token_flow(session):
    token = await webauth.create_login_link_token(session, OWNER_TG, now=NOW)
    assert await webauth.login_token_valid(session, token, now=NOW)
    assert await webauth.login_token_valid(session, token, now=NOW + timedelta(minutes=11)) is None
    sess = await webauth.exchange_login_token(session, token, now=NOW)
    assert sess and await webauth.exchange_login_token(session, token, now=NOW) is None  # одноразовая
    a = await webauth.actor_for_session(session, sess, now=NOW)
    assert a.is_owner and a.channel == Channel.SITE
    assert await webauth.actor_for_session(session, sess, now=NOW + timedelta(days=31)) is None
    await webauth.logout(session, sess)
    assert await webauth.actor_for_session(session, sess, now=NOW) is None


async def test_login_denied_for_unknown_and_inactive(session):
    for tg in (123, INACTIVE):
        with pytest.raises(PermissionDenied):
            await webauth.create_login_link_token(session, tg)


# ---------- HTTP ----------


@pytest.fixture
def client(session):
    async def _session():
        yield session

    web.app.dependency_overrides[web.get_session] = _session

    async def make(tg=None):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test")
        if tg is not None:
            token = await webauth.create_login_link_token(session, tg)
            r = await c.post(f"/login/{token}")
            assert r.status_code == 303 and web.COOKIE in r.cookies
        return c

    yield make
    web.app.dependency_overrides.clear()


async def test_requires_login(client):
    c = await client()
    r = await c.get("/")
    assert r.status_code == 401 and "/site" in r.text
    assert (await c.post("/api/kanban/move", json={})).status_code == 401


async def test_login_get_does_not_consume(session, client):
    c = await client()
    token = await webauth.create_login_link_token(session, OWNER_TG)
    assert (await c.get(f"/login/{token}")).status_code == 200
    assert (await c.get(f"/login/{token}")).status_code == 200
    assert (await c.post(f"/login/{token}")).status_code == 303
    assert (await c.get(f"/login/{token}")).status_code == 410


async def test_empty_site_and_redirect(session, actor, client):
    c = await client(OWNER_TG)
    r = await c.get("/")
    assert r.status_code == 200 and "Сайт пока пустой" in r.text
    await _save(session, await _owner(actor), slug="summary", title="Сводка", blocks=SUMMARY_BLOCKS)
    r = await c.get("/?company=fler")
    assert r.status_code == 303 and r.headers["location"] == "/p/summary?company=fler"


async def test_owner_page_renders_live_numbers(session, actor, client):
    owner = await _owner(actor)
    await _save(session, owner)
    c = await client(OWNER_TG)
    r = await c.get("/p/sales?company=fler&period=half")
    assert r.status_code == 200
    html = r.text
    for s in ["Холдинг", "FLÉR", "Выручка", "Средний чек", "data-chart", "Бэклог", "На проверке", "обновлено"]:
        assert s in html, s
    assert "по компаниям" not in html  # compare_companies на одной компании — обычный план/факт

    # план, изменённый через бота, сразу виден на сайте
    before = (await c.get("/p/sales?company=velse&period=2026-12")).text
    await changes.apply(session, owner, "plans.set_plan",
                        {"company": "velse", "metric": "revenue", "month": "2026-11", "value": 123_400_000})
    after = (await c.get("/p/sales?company=velse&period=month")).text
    assert "123,4 млн ₽" in after and before != after


async def test_holding_compare_chart(session, actor, client):
    await _save(session, await _owner(actor))
    r = await (await client(OWNER_TG)).get("/p/sales?company=holding")
    assert "Выручка по компаниям" in r.text and '"compare": true' in r.text


async def test_employee_sees_own_scope_only(session, actor, client):
    owner = await _owner(actor)
    await _save(session, owner, slug="summary", title="Сводка",
                blocks=[{"type": "summary"}, {"type": "table", "department": "finance"}])
    c = await client(SALES_FLER)
    r = await c.get("/p/summary?company=velse")
    assert r.status_code == 200
    assert "Холдинг" not in r.text and 'href="/p/summary?company=velse' not in r.text  # только FLÉR
    assert "Нет доступа к разделу «Финансы»" in r.text   # чужой отдел — блок закрыт, страница работает
    assert "Выручка" in r.text                            # сводка своей компании видна


async def test_kanban_move_via_site(session, actor, client):
    owner = await _owner(actor)
    await _save(session, owner)
    board = await kanban.board(session, owner, "fler", "sales")
    card = next(c for st, col in board.items() if st != CardStatus.IN_PROGRESS for c in col)
    target = board[CardStatus.IN_PROGRESS]
    assert target, "в демо-данных у ОП FLÉR есть карточки в работе"
    c = await client(SALES_FLER)
    assert (await c.post("/api/kanban/move", json={"card_id": card.id, "status": "in_progress"})).status_code == 400

    n = await _logs(session, "kanban_card")
    r = await c.post("/api/kanban/move", headers={"X-CG": "1"},
                     json={"card_id": card.id, "status": "in_progress", "after_ids": [target[0].id]})
    assert r.status_code == 200, r.text
    await session.refresh(card)
    col = (await kanban.board(session, owner, "fler", "sales"))[CardStatus.IN_PROGRESS]
    assert card.status == CardStatus.IN_PROGRESS and col[0].id == card.id
    row = await session.scalar(select(ChangeLog).order_by(ChangeLog.id.desc()))
    assert await _logs(session, "kanban_card") == n + 1 and row.channel == Channel.SITE

    other = await session.scalar(select(KanbanCard).where(KanbanCard.company_id != card.company_id,
                                                          KanbanCard.department_id == card.department_id))
    r = await c.post("/api/kanban/move", headers={"X-CG": "1"}, json={"card_id": other.id, "status": "done"})
    assert r.status_code == 403


async def test_logout(client):
    c = await client(OWNER_TG)
    assert (await c.post("/logout")).status_code == 303
    assert (await c.get("/")).status_code == 401


# ---------- бот ----------


async def test_site_tools_for_owner_only(session, actor):
    owner, emp = await _owner(actor), await actor(SALES_FLER, Channel.DEPT_BOT)
    assert {"list_pages", "get_page", "save_page", "delete_page", "restore_page"} <= {t.name for t in T.tools_for(owner)}
    assert not {t.name for t in T.tools_for(emp, False)} & {"list_pages", "save_page"}
    allowed = {t.name: t for t in T.tools_for(owner)}
    out = await T.execute(session, owner, "save_page", {"slug": "sales", "title": "ОП", "blocks": SALES_BLOCKS}, allowed)
    assert out.preview and not out.is_error
    await changes.apply(session, owner, out.preview.op, out.preview.params)
    out = await T.execute(session, owner, "get_page", {"slug": "sales"}, allowed)
    assert '"type": "kanban"' in out.content
    out = await T.execute(session, owner, "list_pages", {}, allowed)
    assert "Канбан" in out.content


async def test_page_links_after_confirm(session, actor):
    owner = await _owner(actor)
    p = await _save(session, owner)
    links = page_links([p])
    assert len(links) == 1 and links[0].endswith("/p/sales")
