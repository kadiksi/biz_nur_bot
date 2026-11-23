"""Внешние сервисы (любые HTTP API) и Google Таблицы.

Бот ходит только по зарегистрированным адресам (подключениям). Ключи подставляются здесь,
из .env, и в ответы бота не попадают. Чтение — сразу; любой запрос, который что-то меняет
во внешнем сервисе, — только после кнопки «Подтвердить». 1С сюда не подключается: она только для чтения.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import onec as onec_api
from app.models import ServiceConnection
from app.services import changelog, secrets
from app.services.access import require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import NotFound, ServiceError, ValidationError

AUTHS = {
    "none": "без ключа",
    "bearer": "заголовок Authorization: Bearer <ключ>",
    "header": "свой заголовок <param>: <ключ>",
    "query": "параметр ?<param>=<ключ> в адресе",
    "basic": "логин и пароль (secret_user — логин, secret — пароль)",
    "url": "ключ внутри адреса: {secret} в base_url (вебхуки Битрикс24 и т. п.)",
    "google": "сервисный аккаунт Google (secret — JSON-файл ключа)",
}
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
MAX_BODY = 30_000
TIMEOUT = 30
GOOGLE_SCOPES = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.readonly"

TRANSPORT: httpx.AsyncBaseTransport | None = None  # тесты подменяют


class ExternalError(ServiceError):
    pass


# ---------- проверки ----------


def _onec_hosts() -> set[str]:
    return {(urlsplit(c.url).hostname or "").lower() for c in onec_api.load_connections().values()}


def check_base_url(url: str, auth: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url.replace("{secret}", "x"))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError("Адрес сервиса должен начинаться с https:// (или http://)")
    if parts.username or parts.password:
        raise ValidationError("Логин и пароль в адресе не пишем — для этого есть ключи в .env (auth=basic)")
    if parts.query or parts.fragment:
        raise ValidationError("В базовом адресе не должно быть ? и # — параметры передаются в запросе")
    if "{secret}" in url and auth != "url":
        raise ValidationError("{secret} в адресе — только для auth=url")
    if auth == "url" and "{secret}" not in url:
        raise ValidationError("Для auth=url поставьте {secret} в адрес там, где должен быть ключ")
    if parts.hostname.lower() in _onec_hosts():
        raise ValidationError("Это сервер 1С. С 1С работаем только на чтение, через onec_* инструменты")
    return url


def _check_path(path: str | None) -> str:
    path = (path or "").strip()
    if "://" in path or path.startswith("//") or ".." in path.split("?")[0].split("/") or "#" in path:
        raise ValidationError("path — только путь внутри сервиса, например /v1/leads")
    return path


async def get_connection(session: AsyncSession, name: str) -> ServiceConnection:
    conn = await session.scalar(select(ServiceConnection).where(ServiceConnection.name == (name or "").strip().lower()))
    if conn is None:
        raise NotFound(f"Подключение «{name}» не найдено. Сначала сохраните его (save_service)")
    return conn


def _sa_info(key: str) -> dict:
    raw = secrets.get(key)
    if not raw:
        raise ExternalError(f"Ключа {key} ещё нет в .env — запросите его у владельца (request_secret)")
    try:
        info = json.loads(raw)
    except ValueError:
        raise ExternalError(f"{key} — не JSON-файл сервисного аккаунта Google") from None
    if info.get("type") != "service_account":
        raise ExternalError(f"{key} — не ключ сервисного аккаунта Google (type != service_account)")
    return info


def service_account_email(conn: ServiceConnection) -> str | None:
    if conn.auth != "google" or not conn.secret:
        return None
    try:
        return _sa_info(conn.secret).get("client_email")
    except ExternalError:
        return None


# ---------- список и сохранение подключений ----------

_FIELDS = ["name", "title", "base_url", "auth", "secret", "secret_user", "param", "allow_write", "notes"]
_LABELS = {"title": "Название", "base_url": "Адрес", "auth": "Вход", "secret": "Ключ в .env",
           "secret_user": "Логин в .env", "param": "Заголовок/параметр", "allow_write": "Разрешены изменения",
           "notes": "Заметка"}


async def list_connections(session: AsyncSession, actor: Actor) -> list[dict]:
    require_owner(actor, "работать с внешними сервисами")
    have = set(secrets.names())
    out = []
    for c in await session.scalars(select(ServiceConnection).order_by(ServiceConnection.name)):
        item = {k: getattr(c, k) for k in _FIELDS}
        for k in ("secret", "secret_user"):
            if getattr(c, k):
                item[f"{k}_present"] = getattr(c, k) in have
        if email := service_account_email(c):
            item["service_account_email"] = email
        out.append(item)
    return out


def _show(k: str, v: Any) -> str:
    if k == "allow_write":
        return "да" if v else "нет"
    if k == "auth":
        return f"{v} — {AUTHS.get(v, v)}"
    return "—" if v in (None, "") else str(v)


@write_op("services.save")
async def save_connection(session: AsyncSession, actor: Actor, *, name: str, title: str, base_url: str,
                          auth: str = "none", secret: str | None = None, secret_user: str | None = None,
                          param: str | None = None, allow_write: bool = False, notes: str | None = UNSET,
                          dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "подключать внешние сервисы")
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValidationError("Имя подключения — латиница в нижнем регистре, цифры, _ и -: amocrm, gsheets")
    if auth not in AUTHS:
        raise ValidationError(f"auth: {', '.join(AUTHS)}")
    title = (title or "").strip()
    if not title or len(title) > 100:
        raise ValidationError("Название — до 100 символов")
    new: dict[str, Any] = {"title": title, "auth": auth, "allow_write": bool(allow_write)}
    new["base_url"] = check_base_url(base_url, auth) if auth != "google" else (
        check_base_url(base_url or "https://sheets.googleapis.com", "none"))
    new["secret"] = secrets.check_key(secret) if secret else None
    new["secret_user"] = secrets.check_key(secret_user) if secret_user else None
    new["param"] = (param or "").strip() or None
    if auth != "none" and not new["secret"]:
        raise ValidationError("Укажите secret — имя ключа в .env (если ключа нет, запросите его: request_secret)")
    if auth == "basic" and not new["secret_user"]:
        raise ValidationError("Для basic нужен secret_user — имя ключа с логином")
    if auth in ("header", "query") and not (new["param"] and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", new["param"])):
        raise ValidationError("Для header/query нужен param — имя заголовка или параметра (X-Api-Key, apikey)")
    row = await session.scalar(select(ServiceConnection).where(ServiceConnection.name == name))
    if notes is not UNSET:
        new["notes"] = (notes or "").strip() or None
    elif row is None:
        new["notes"] = None
    old = changelog.snapshot(row, _FIELDS) if row else {}
    diff = [FieldChange(_LABELS[k], _show(k, old.get(k)) if row else None, _show(k, v))
            for k, v in new.items() if (row and old.get(k) != v) or (not row and v not in (None, ""))]
    if row and not diff:
        raise ValidationError("Подключение уже настроено так — менять нечего")
    preview = ChangePreview(title=f"Подключение к сервису «{title}» ({name})", changes=diff)
    if dry_run:
        return preview
    if row is None:
        row = ServiceConnection(name=name)
        session.add(row)
    for k, v in new.items():
        setattr(row, k, v)
    await session.flush()
    await changelog.log(session, actor, entity="service_connection", entity_id=row.id,
                        action="update" if old else "create", summary=preview.render(),
                        before=old or None, after=changelog.snapshot(row, _FIELDS))
    await session.commit()
    return preview


@write_op("services.delete")
async def delete_connection(session: AsyncSession, actor: Actor, *, name: str, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "удалять подключения")
    row = await get_connection(session, name)
    preview = ChangePreview(title=f"Удалить подключение «{row.title}» ({row.name})",
                            changes=[FieldChange("Адрес", row.base_url, None),
                                     FieldChange("Ключи в .env", None, "останутся, их можно удалить вручную")])
    if dry_run:
        return preview
    await changelog.log(session, actor, entity="service_connection", entity_id=row.id, action="delete",
                        summary=preview.render(), before=changelog.snapshot(row, _FIELDS))
    await session.delete(row)
    await session.commit()
    return preview


# ---------- запросы ----------

_google_tokens: dict[str, tuple[float, str]] = {}


async def _google_token(key: str) -> str:
    hit = _google_tokens.get(key)
    if hit and hit[0] > time.time() + 60:
        return hit[1]
    from google.auth import crypt, jwt

    info = _sa_info(key)
    now = int(time.time())
    token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    assertion = jwt.encode(crypt.RSASigner.from_service_account_info(info), {
        "iss": info["client_email"], "scope": GOOGLE_SCOPES, "aud": token_uri, "iat": now, "exp": now + 3600})
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT) as http:
        r = await http.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                             "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
    if r.status_code != 200:
        raise ExternalError(f"Google не выдал доступ по ключу {key}: {r.text[:300]}")
    data = r.json()
    _google_tokens[key] = (now + int(data.get("expires_in", 3600)), data["access_token"])
    return data["access_token"]


async def _auth(conn: ServiceConnection, url: str, headers: dict, params: dict) -> tuple[str, Any]:
    def need(k: str) -> str:
        v = secrets.get(k)
        if not v:
            raise ExternalError(f"Ключа {k} ещё нет в .env — запросите его у владельца (request_secret)")
        return v

    auth = None
    if conn.auth == "bearer":
        headers["Authorization"] = f"Bearer {need(conn.secret)}"
    elif conn.auth == "header":
        headers[conn.param] = need(conn.secret)
    elif conn.auth == "query":
        params[conn.param] = need(conn.secret)
    elif conn.auth == "basic":
        auth = httpx.BasicAuth(need(conn.secret_user), need(conn.secret))
    elif conn.auth == "url":
        url = url.replace("{secret}", quote(need(conn.secret), safe=""))
    elif conn.auth == "google":
        headers["Authorization"] = f"Bearer {await _google_token(conn.secret)}"
    return url, auth


async def _send(conn: ServiceConnection, method: str, path: str, query: dict | None, body: Any,
                base: str | None = None) -> dict:
    url = (base or conn.base_url).rstrip("/") + ("/" + _check_path(path).lstrip("/") if path else "")
    headers: dict[str, str] = {"Accept": "application/json, text/plain, */*"}
    params = {str(k): v if isinstance(v, str) else json.dumps(v) if isinstance(v, dict | list) else str(v)
              for k, v in (query or {}).items()}
    url, auth = await _auth(conn, url, headers, params)
    kwargs: dict[str, Any] = {"params": params, "headers": headers, "auth": auth}
    if body is not None:
        if isinstance(body, dict | list):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body).encode()
            headers["Content-Type"] = "text/plain; charset=utf-8"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT, follow_redirects=False) as http:
            r = await http.request(method, url, **kwargs)
    except httpx.TimeoutException:
        raise ExternalError(f"«{conn.title}» не ответил за {TIMEOUT} с") from None
    except httpx.HTTPError as e:
        raise ExternalError(f"Нет связи с «{conn.title}»: {e.__class__.__name__}") from None
    keys = [k for k in (conn.secret, conn.secret_user) if k]
    text = secrets.redact(r.text, keys)
    out: dict[str, Any] = {"status": r.status_code, "content_type": r.headers.get("content-type", "")}
    if r.headers.get("location"):
        out["redirect_to"] = secrets.redact(r.headers["location"], keys)
    try:
        data = json.loads(text) if "json" in out["content_type"] or text[:1] in "[{" else None
    except ValueError:
        data = None
    if data is not None and len(text) <= MAX_BODY:
        out["json"] = data
    else:
        out["text"] = text[:MAX_BODY] + (f"… [обрезано, всего {len(text)} символов]" if len(text) > MAX_BODY else "")
    return out


async def request_read(session: AsyncSession, actor: Actor, *, connection: str, path: str = "",
                       query: dict | None = None, method: str = "GET") -> dict:
    require_owner(actor, "работать с внешними сервисами")
    method = (method or "GET").upper()
    if method not in ("GET", "HEAD"):
        raise ValidationError("Здесь только чтение (GET). Изменения — через service_write с подтверждением")
    return await _send(await get_connection(session, connection), method, path, query, None)


def _short(v: Any, n: int = 400) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


@write_op("services.request", external=True)
async def request_write(session: AsyncSession, actor: Actor, *, connection: str, method: str, path: str = "",
                        query: dict | None = None, body: Any = None, purpose: str = "",
                        dry_run: bool = False) -> ChangePreview:
    """Запрос, который что-то меняет во внешнем сервисе. Выполняется только после «Подтвердить»."""
    require_owner(actor, "менять данные во внешних сервисах")
    conn = await get_connection(session, connection)
    method = (method or "").upper()
    if method not in WRITE_METHODS:
        raise ValidationError(f"method: {', '.join(WRITE_METHODS)} (для чтения — service_request)")
    if not conn.allow_write:
        raise ValidationError(f"В подключении «{conn.title}» изменения не разрешены (allow_write=false). "
                              "Включить можно через save_service")
    _check_path(path)
    changes = [FieldChange("Что делаем", None, purpose.strip() or "—"),
               FieldChange("Запрос", None, f"{method} {conn.base_url.replace('{secret}', '***')}"
                                               f"{('/' + path.lstrip('/')) if path else ''}")]
    if query:
        changes.append(FieldChange("Параметры", None, _short(query)))
    if body is not None:
        changes.append(FieldChange("Данные", None, _short(body)))
    preview = ChangePreview(title=f"Изменение во внешнем сервисе «{conn.title}»", changes=changes)
    if dry_run:
        return preview
    res = await _send(conn, method, path, query, body)
    ok = 200 <= res["status"] < 300
    await changelog.log(session, actor, entity="service_request", entity_id=conn.id, action=method.lower(),
                        summary=preview.render() + f"\n• Ответ: {res['status']}",
                        after={"status": res["status"], "response": _short(res.get("json", res.get("text")), 2000)})
    await session.commit()
    preview.title += f" → ответ {res['status']}" + ("" if ok else f": {_short(res.get('json', res.get('text')), 300)}")
    return preview


# ---------- Google Таблицы ----------

_SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})|^([a-zA-Z0-9_-]{20,})$")
_GID = re.compile(r"[#&?]gid=(\d+)")
MAX_ROWS = 500


def sheet_id(spreadsheet: str) -> str:
    m = _SHEET_ID.search((spreadsheet or "").strip())
    if not m:
        raise ValidationError("Не похоже на ссылку на Google Таблицу или её id")
    return m.group(1) or m.group(2)


def _rows(values: list[list[Any]]) -> dict:
    return {"rows": values[:MAX_ROWS], "total_rows": len(values),
            **({"note": f"показаны первые {MAX_ROWS} строк — уточните диапазон"} if len(values) > MAX_ROWS else {})}


async def _google_conn(session: AsyncSession, name: str) -> ServiceConnection:
    conn = await get_connection(session, name)
    if conn.auth != "google":
        raise ValidationError(f"«{conn.title}» — не подключение Google (auth=google)")
    return conn


def _google_error(res: dict, spreadsheet: str, email: str | None) -> None:
    if res["status"] in (403, 404):
        raise ExternalError("Нет доступа к таблице. Владелец должен открыть таблицу → «Настройки доступа» → "
                            f"добавить {email or 'адрес сервисного аккаунта'} (читатель или редактор)")
    if res["status"] >= 400:
        raise ExternalError(f"Google ответил {res['status']}: {_short(res.get('json', res.get('text')), 300)}")


async def sheets_read(session: AsyncSession, actor: Actor, *, spreadsheet: str, range: str | None = None,
                      connection: str | None = None) -> dict:
    """Прочитать лист. Без connection — таблица должна быть открыта «всем, у кого есть ссылка»."""
    require_owner(actor, "читать Google Таблицы")
    sid = sheet_id(spreadsheet)
    if connection:
        conn = await _google_conn(session, connection)
        email = service_account_email(conn)
        meta = await _send(conn, "GET", f"/v4/spreadsheets/{sid}", {"fields": "properties.title,sheets.properties"},
                           None, base="https://sheets.googleapis.com")
        _google_error(meta, spreadsheet, email)
        sheets = [s["properties"]["title"] for s in meta["json"].get("sheets", [])]
        rng = range or (f"'{sheets[0]}'" if sheets else "A1:Z1000")
        res = await _send(conn, "GET", f"/v4/spreadsheets/{sid}/values/{quote(rng, safe='')}",
                          {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
                          None, base="https://sheets.googleapis.com")
        _google_error(res, spreadsheet, email)
        return {"title": meta["json"].get("properties", {}).get("title"), "sheets": sheets, "range": res["json"].get("range"),
                **_rows(res["json"].get("values", []))}
    # открытая по ссылке таблица: выгрузка CSV без ключа
    params = {"tqx": "out:csv"}
    if range:
        params["sheet"] = range.split("!")[0].strip("'")
    elif m := _GID.search(spreadsheet):
        params["gid"] = m.group(1)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT, follow_redirects=True) as http:
            r = await http.get(f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq", params=params)
    except httpx.HTTPError as e:
        raise ExternalError(f"Нет связи с Google: {e.__class__.__name__}") from None
    if r.status_code != 200 or "text/csv" not in r.headers.get("content-type", ""):
        raise ExternalError("Таблица закрыта. Либо откройте доступ «всем, у кого есть ссылка» (просмотр), либо "
                            "подключите сервисный аккаунт Google (save_service с auth=google)")
    return _rows(list(csv.reader(io.StringIO(r.text))))


@write_op("sheets.write", external=True)
async def sheets_write(session: AsyncSession, actor: Actor, *, connection: str, spreadsheet: str, range: str,
                       values: list[list[Any]], mode: str = "update", dry_run: bool = False) -> ChangePreview:
    """Записать строки в Google Таблицу: update — в указанный диапазон, append — добавить в конец."""
    require_owner(actor, "менять Google Таблицы")
    conn = await _google_conn(session, connection)
    sid = sheet_id(spreadsheet)
    if mode not in ("update", "append"):
        raise ValidationError("mode: update (в диапазон) или append (добавить строки в конец)")
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
        raise ValidationError("values — список строк, каждая строка — список ячеек")
    if len(values) > 5000:
        raise ValidationError("Не больше 5000 строк за раз")
    preview = ChangePreview(
        title=f"Запись в Google Таблицу · {range}",
        changes=[FieldChange("Как", None, "заменить диапазон" if mode == "update" else "добавить строки в конец"),
                 FieldChange("Строк", None, str(len(values)))]
        + [FieldChange(f"Строка {i}", None, _short(r, 150)) for i, r in enumerate(values[:5], 1)])
    if dry_run:
        return preview
    path = f"/v4/spreadsheets/{sid}/values/{quote(range, safe='')}" + (":append" if mode == "append" else "")
    res = await _send(conn, "POST" if mode == "append" else "PUT", path,
                      {"valueInputOption": "USER_ENTERED", **({"insertDataOption": "INSERT_ROWS"} if mode == "append" else {})},
                      {"values": values}, base="https://sheets.googleapis.com")
    _google_error(res, spreadsheet, service_account_email(conn))
    await changelog.log(session, actor, entity="google_sheet", entity_id=None, action=mode,
                        summary=preview.render(), after={"spreadsheet": sid, "range": range, "rows": len(values)})
    await session.commit()
    return preview
