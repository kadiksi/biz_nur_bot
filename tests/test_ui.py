import json

import pytest

from app.agent import tools as T
from app.models import Channel
from app.services import changes, sitecfg, site
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import OWNER_TG, SALES_HEAD
from tests.test_site import client  # noqa: F401 — фикстура


@pytest.fixture
async def owner(actor):
    return await actor(OWNER_TG, Channel.MAIN_BOT)


@pytest.mark.parametrize("css", [
    ".a{background:url(http://evil/x)}", "</style><script>alert(1)</script>", "@import 'x.css';",
    ".a{background:image-set('http://x' 1x)}", ".a{content:'\\3c'}", ".a{color:red", "@font-face{font-family:x}",
])
def test_css_rejected(css):
    with pytest.raises(ValidationError):
        sitecfg.check_css(css)


async def test_css_versions_and_site(session, owner, actor, client):  # noqa: F811
    await site.save_page(session, owner, slug="summary", title="Сводка", blocks=[{"type": "summary"}])
    p = await changes.apply(session, owner, "site.set_css", {"css": ".fresh{display:none}"})
    assert "Добавлено: .fresh{display:none}" in p.render()
    await changes.apply(session, owner, "site.set_css", {"css": ".fresh{display:none}\n.kpi__fact{font-size:40px}"})
    with pytest.raises(ValidationError, match="не изменились"):
        await changes.preview(session, owner, "site.set_css", {"css": ".fresh{display:none}\n.kpi__fact{font-size:40px}"})
    hist = await sitecfg.css_history(session)
    assert [h["css"] for h in hist] == [".fresh{display:none}"]

    c = await client(OWNER_TG)
    r = await c.get("/p/summary")
    assert '<style id="custom-css">.fresh{display:none}\n.kpi__fact{font-size:40px}</style>' in r.text

    await changes.apply(session, owner, "site.restore_css", {"version": 1})
    assert (await sitecfg.get_all(session))["custom_css"] == ".fresh{display:none}"
    await changes.apply(session, owner, "site.set_css", {"css": ""})
    assert "custom-css" not in (await c.get("/p/summary")).text

    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await changes.preview(session, head, "site.set_css", {"css": ".a{}"})


async def test_texts(session, owner, client):  # noqa: F811
    await site.save_page(session, owner, slug="summary", title="Сводка", blocks=[{"type": "kpis", "metrics": ["revenue"]}])
    with pytest.raises(ValidationError, match="Нет таких надписей"):
        await changes.preview(session, owner, "site.update_texts", {"texts": {"nope": "x"}})
    p = await changes.apply(session, owner, "site.update_texts",
                            {"texts": {"holding": "Вся группа", "kpi_plan": "цель", "sync_done": "Готово!"}})
    assert "holding: Холдинг → Вся группа" in p.render()
    c = await client(OWNER_TG)
    r = await c.get("/p/summary")
    assert "Вся группа" in r.text and "цель " in r.text and "Холдинг" not in r.text
    texts = json.loads(r.text.split('<script type="application/json" id="ui-texts">')[1].split("</script>")[0])
    assert texts["sync_done"] == "Готово!"
    await changes.apply(session, owner, "site.update_texts", {"texts": {"holding": None}})
    assert "Холдинг" in (await c.get("/p/summary")).text


async def test_progress_bar_markup(session, owner, client):  # noqa: F811
    await site.save_page(session, owner, slug="summary", title="Сводка", blocks=[{"type": "summary"}])
    r = await (await client(OWNER_TG)).get("/p/summary")
    assert 'class="sync" data-sync hidden' in r.text and "data-sync-fill" in r.text
    assert "sync-note" not in r.text  # вращающегося значка больше нет


async def test_ui_map_tool(session, owner):
    allowed = {t.name: t for t in T.tools_for(owner)}
    out = await T.execute(session, owner, "get_ui_map", {}, allowed)
    data = json.loads(out.content)
    assert any(".sync" in e["selector"] for e in data["elements"])
    assert {"key": "holding", "default": "Холдинг"} in data["texts"]
    out = await T.execute(session, owner, "set_site_css", {"css": ".kpi{border-radius:0}"}, allowed)
    assert out.preview and "НЕ применено" in out.content
