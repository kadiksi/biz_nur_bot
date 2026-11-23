import json
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.agent import tools as T
from app.integrations import onec as onec_api
from app.models import Channel, ChangeLog
from app.services import changes, connectors, secrets
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import OWNER_TG, SALES_HEAD
from tests.test_site import client  # noqa: F401 — фикстура

SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("BOT_TOKEN_MAIN=keep-me\n# комментарий\nONEC_UT_URL=http://1c.local/ut/odata/standard.odata\n")
    monkeypatch.setattr(secrets, "ENV_PATH", p)
    monkeypatch.setattr(onec_api, "_env", lambda: {"ONEC_UT_URL": "http://1c.local/ut/odata/standard.odata"})
    for k in list(__import__("os").environ):
        if k.startswith(("AMO_", "GS_")):
            monkeypatch.delenv(k)
    return p


@pytest.fixture
async def owner(actor):
    return await actor(OWNER_TG, Channel.MAIN_BOT)


class FakeWeb:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        self.sa = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
                   "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token"}

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        u = req.url
        if u.host == "api.crm.test":
            if req.headers.get("authorization") != "Bearer tok-123456":
                return httpx.Response(401, json={"error": "unauthorized"})
            if req.method == "GET":
                return httpx.Response(200, json={"leads": [{"id": 1, "echo": "tok-123456"}]})
            return httpx.Response(201, json={"created": json.loads(req.content)})
        if u.host == "oauth2.googleapis.com":
            form = parse_qs(req.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            return httpx.Response(200, json={"access_token": "g-token", "expires_in": 3600})
        if u.host == "sheets.googleapis.com":
            assert req.headers["authorization"] == "Bearer g-token"
            if u.path == f"/v4/spreadsheets/{SHEET}":
                return httpx.Response(200, json={"properties": {"title": "Планы"},
                                                 "sheets": [{"properties": {"title": "2026"}}]})
            if req.method == "PUT":
                return httpx.Response(200, json={"updatedRows": 1})
            return httpx.Response(200, json={"range": "'2026'!A1:B3", "values": [["Месяц", "Выручка"], ["2026-12", 25000000]]})
        if u.host == "docs.google.com":
            if SHEET in u.path:
                return httpx.Response(200, text="Месяц,Выручка\n2026-12,25000000\n", headers={"content-type": "text/csv"})
            return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
        return httpx.Response(404)


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(connectors, "TRANSPORT", httpx.MockTransport(w.handle))
    connectors._google_tokens.clear()
    return w


# ---------- .env и запросы ключей ----------


def test_env_write_keeps_other_lines(env):
    secrets._write("AMO_TOKEN", "abc-123")
    secrets._write("GS_KEY", '{"type": "x", "a": "b c"}')
    secrets._write("AMO_TOKEN", "new-456")
    text = env.read_text()
    assert "BOT_TOKEN_MAIN=keep-me" in text and "# комментарий" in text and text.count("AMO_TOKEN=") == 1
    assert "GS_KEY=b64:" in text and '"b c"' not in text
    assert secrets.get("AMO_TOKEN") == "new-456" and json.loads(secrets.get("GS_KEY"))["a"] == "b c"
    assert secrets.names() == ["AMO_TOKEN", "GS_KEY"]  # служебные не показываем
    assert oct(env.stat().st_mode)[-3:] == "600"


@pytest.mark.parametrize("key", ["BOT_TOKEN_MAIN", "ANTHROPIC_API_KEY", "ONEC_UT_PASSWORD", "bad key", "SITE_URL"])
def test_protected_keys(key):
    with pytest.raises(ValidationError):
        secrets.check_key(key)


async def test_secret_request_via_site(session, owner, actor, env, client):  # noqa: F811
    got = []

    async def listener(key, title):
        got.append((key, title))

    secrets._listeners.append(listener)
    try:
        req = await secrets.create_request(session, owner, key="AMO_TOKEN", title="Токен amoCRM",
                                           instructions="Откройте amoCRM\nНастройки → Интеграции")
        again = await secrets.create_request(session, owner, key="amo_token", title="Токен amoCRM")
        assert again.public_id == req.public_id  # повторный запрос того же ключа — та же ссылка

        emp = await client(SALES_HEAD)
        assert (await emp.get(f"/secret/{req.public_id}")).status_code == 404
        c = await client(OWNER_TG)
        r = await c.get(f"/secret/{req.public_id}")
        assert "Токен amoCRM" in r.text and "Настройки → Интеграции" in r.text and 'type="password"' in r.text
        r = await c.post(f"/secret/{req.public_id}", data={"value": "  tok-123456  "})
        assert r.status_code == 200 and "Сохранено" in r.text and "tok-123456" not in r.text
        assert secrets.get("AMO_TOKEN") == "tok-123456"
        import asyncio
        await asyncio.sleep(0.05)
        assert got == [("AMO_TOKEN", "Токен amoCRM")]
        r = await c.post(f"/secret/{req.public_id}", data={"value": "other"})
        assert "Уже передано" in r.text and secrets.get("AMO_TOKEN") == "tok-123456"
        log = (await session.scalars(select(ChangeLog.summary).where(ChangeLog.entity == "secret"))).all()
        assert log and not any("tok-123456" in s for s in log)
    finally:
        secrets._listeners.remove(listener)


async def test_json_secret_upload(session, owner, env, client, web):  # noqa: F811
    req = await secrets.create_request(session, owner, key="GS_KEY", title="Ключ Google", kind="json")
    c = await client(OWNER_TG)
    r = await c.post(f"/secret/{req.public_id}", files={"file": ("k.json", b"not json")}, data={"value": ""})
    assert "Это не JSON" in r.text
    r = await c.post(f"/secret/{req.public_id}", files={"file": ("k.json", json.dumps(web.sa, indent=2).encode())},
                     data={"value": ""})
    assert "Сохранено" in r.text and json.loads(secrets.get("GS_KEY"))["client_email"] == web.sa["client_email"]


# ---------- подключения ----------


async def test_save_connection_rules(session, owner, env):
    bad = [
        (dict(name="x1", title="1С", base_url="http://1c.local/other", auth="none"), "сервер 1С"),
        (dict(name="x2", title="X", base_url="ftp://a.b", auth="none"), "https://"),
        (dict(name="x3", title="X", base_url="https://u:p@a.b", auth="none"), "Логин и пароль"),
        (dict(name="x4", title="X", base_url="https://a.b", auth="bearer"), "secret"),
        (dict(name="x5", title="X", base_url="https://a.b/{secret}", auth="bearer", secret="AMO_TOKEN"), "auth=url"),
        (dict(name="x6", title="X", base_url="https://a.b", auth="header", secret="AMO_TOKEN"), "param"),
        (dict(name="x7", title="X", base_url="https://a.b", auth="bearer", secret="BOT_TOKEN_MAIN"), "служебная"),
    ]
    for params, msg in bad:
        with pytest.raises(ValidationError, match=msg):
            await changes.preview(session, owner, "services.save", params)


async def test_request_read_and_write(session, owner, env, web):
    secrets._write("AMO_TOKEN", "tok-123456")
    await changes.apply(session, owner, "services.save", dict(name="amocrm", title="amoCRM", base_url="https://api.crm.test/api",
                                                              auth="bearer", secret="AMO_TOKEN"))
    res = await connectors.request_read(session, owner, connection="amocrm", path="/v4/leads", query={"limit": 5})
    assert res["status"] == 200 and res["json"]["leads"][0]["echo"] == "[скрыто]"  # токен не утекает в ответ
    assert str(web.requests[-1].url) == "https://api.crm.test/api/v4/leads?limit=5"
    with pytest.raises(ValidationError, match="только путь"):
        await connectors.request_read(session, owner, connection="amocrm", path="https://evil.test/x")
    with pytest.raises(ValidationError, match="только чтение"):
        await connectors.request_read(session, owner, connection="amocrm", method="POST")

    params = dict(connection="amocrm", method="POST", path="/v4/leads", body=[{"name": "Сделка"}], purpose="Создать сделку")
    with pytest.raises(ValidationError, match="allow_write"):
        await changes.preview(session, owner, "services.request", params)
    await changes.apply(session, owner, "services.save", dict(name="amocrm", title="amoCRM", base_url="https://api.crm.test/api",
                                                              auth="bearer", secret="AMO_TOKEN", allow_write=True))
    n = len(web.requests)
    p = await changes.preview(session, owner, "services.request", params)
    assert len(web.requests) == n  # предпросмотр ничего не отправляет
    assert "POST https://api.crm.test/api/v4/leads" in p.render()
    done = await changes.apply(session, owner, "services.request", params)
    assert done.title.endswith("ответ 201") and web.requests[-1].method == "POST"

    lst = await connectors.list_connections(session, owner)
    assert lst[0]["secret_present"] is True and "tok-123456" not in json.dumps(lst)


async def test_owner_only(session, actor, env):
    head = await actor(SALES_HEAD, Channel.DEPT_BOT)
    with pytest.raises(PermissionDenied):
        await connectors.list_connections(session, head)
    with pytest.raises(PermissionDenied):
        await secrets.create_request(session, head, key="X_KEY", title="x")
    assert not {"request_secret", "service_request", "sheets_read"} & {t.name for t in T.tools_for(head)}


async def test_sheets(session, owner, env, web):
    pub = await connectors.sheets_read(session, owner, spreadsheet=f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=0")
    assert pub["rows"] == [["Месяц", "Выручка"], ["2026-12", "25000000"]]
    from app.services.connectors import ExternalError
    with pytest.raises(ExternalError, match="Таблица закрыта"):
        await connectors.sheets_read(session, owner, spreadsheet="1ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ")

    secrets._write("GS_KEY", json.dumps(web.sa))
    await changes.apply(session, owner, "services.save", dict(name="gsheets", title="Google", base_url="", auth="google",
                                                              secret="GS_KEY"))
    lst = await connectors.list_connections(session, owner)
    assert lst[0]["service_account_email"] == "bot@proj.iam.gserviceaccount.com"
    data = await connectors.sheets_read(session, owner, spreadsheet=SHEET, connection="gsheets")
    assert data["title"] == "Планы" and data["rows"][1] == ["2026-12", 25000000]
    p = await changes.apply(session, owner, "sheets.write", dict(connection="gsheets", spreadsheet=SHEET, range="'2026'!A3",
                                                                 values=[["2027-01", 26000000]]))
    assert "Строк: 1" in p.render() and web.requests[-1].method == "PUT"


async def test_tools(session, owner, env, web):
    allowed = {t.name: t for t in T.tools_for(owner)}
    out = await T.execute(session, owner, "request_secret", {"key": "AMO_TOKEN", "title": "Токен amoCRM"}, allowed)
    data = json.loads(out.content)
    assert "/secret/" in data["link"] and not out.is_error
    out = await T.execute(session, owner, "list_services", {}, allowed)
    assert json.loads(out.content)["waiting_for_owner"] == [{"key": "AMO_TOKEN", "title": "Токен amoCRM"}]


# ---------- новая база 1С через сайт ----------


async def test_onec_connection_via_site(session, owner, actor, env, client, monkeypatch):  # noqa: F811
    from tests import fake_onec

    monkeypatch.setattr(onec_api, "_env", _real_env)  # настоящее чтение .env (фикстура env его подменила)
    monkeypatch.setattr(onec_api, "TRANSPORT", fake_onec.FakeOneC().transport())
    got = []

    async def listener(key, title):
        got.append(key)

    with pytest.raises(ValidationError, match="уже есть"):  # ut уже в .env — не трогаем
        await secrets.create_onec_request(session, owner, name="ut")
    with pytest.raises(ValidationError):
        await secrets.create_onec_request(session, owner, name="Велсе")
    with pytest.raises(PermissionDenied):
        await secrets.create_onec_request(session, await actor(SALES_HEAD, Channel.DEPT_BOT), name="velse")

    out = await T.execute(session, owner, "onec_request_connection",
                          {"name": "velse", "url": fake_onec.BASE, "user": fake_onec.USER},
                          {t.name: t for t in T.tools_for(owner)})
    data = json.loads(out.content)
    pid = data["link"].rsplit("/", 1)[1]
    assert data["connection"] == "velse" and not out.is_error

    secrets._listeners.append(listener)
    try:
        c = await client(OWNER_TG)
        r = await c.get(f"/secret/{pid}")
        assert fake_onec.BASE in r.text and 'name="password"' in r.text
        # неверный пароль — в .env ничего не пишется, адрес и логин остаются в форме
        r = await c.post(f"/secret/{pid}", data={"url": fake_onec.BASE, "user": fake_onec.USER, "password": "wrong"})
        assert "Не удалось подключиться" in r.text and fake_onec.BASE in r.text and "wrong" not in r.text
        assert "ONEC_VELSE" not in env.read_text()
        pw = "s3cret pass!"  # с пробелом — запишется как b64
        monkeypatch.setattr(fake_onec, "PASSWORD", pw)
        r = await c.post(f"/secret/{pid}", data={"url": fake_onec.BASE + "/", "user": fake_onec.USER, "password": pw})
        assert "Сохранено" in r.text and pw not in r.text
        text = env.read_text()
        assert "BOT_TOKEN_MAIN=keep-me" in text and "ONEC_UT_URL=" in text and pw not in text
        conn = onec_api.load_connections()["velse"]
        assert conn.url == fake_onec.BASE and conn.user == fake_onec.USER and conn.password == pw
        import asyncio
        await asyncio.sleep(0.05)
        assert got == ["ONEC_VELSE"]
        log = (await session.scalars(select(ChangeLog.summary).where(ChangeLog.entity == "secret"))).all()
        assert any("velse" in s for s in log) and not any(pw in s for s in log)
        with pytest.raises(ValidationError, match="уже есть"):
            await secrets.create_onec_request(session, owner, name="velse")
    finally:
        secrets._listeners.remove(listener)


_real_env = onec_api._env
