"""Сайт: страницы, которые собирает главный бот.

    python -m app.web          # только сайт
    python -m app.run          # сайт + главный бот
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_sessionmaker
from app.models import CardStatus, Company, Department
from app.services import changes, kanban, onec, refs, secrets, site, sitecfg, sync, webauth
from app.services.context import Actor
from app.services.errors import NotFound, PermissionDenied, ServiceError, ValidationError
from app.services.periods import Period
from app.web import blocks as B

HERE = Path(__file__).parent
COOKIE = "cg_session"

templates = Jinja2Templates(directory=HERE / "templates")

app = FastAPI(title="Corner Group · Оперативные данные", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def site_url() -> str:
    if settings.site_url:
        return settings.site_url.rstrip("/")
    ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # пакет не отправляется — только выбор интерфейса
            ip = s.getsockname()[0]
    except OSError:
        pass
    return f"http://{ip}:{settings.site_port}"


def check_port_free(wait: float = 20) -> None:
    """Понятная ошибка вместо трассировки uvicorn, если сайт уже запущен.
    Ждёт до wait секунд: при перезапуске службы старый процесс ещё освобождает порт."""
    import time

    deadline = time.monotonic() + wait
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((settings.site_host, settings.site_port))
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise SystemExit(f"Порт {settings.site_port} занят — сайт уже запущен в другом окне? "
                                     "Закройте его или поменяйте SITE_PORT в .env") from None
        time.sleep(0.5)


# ---------- зависимости ----------


async def get_session():
    async with get_sessionmaker()() as s:
        yield s


Session = Annotated[AsyncSession, Depends(get_session)]


async def current_actor(request: Request, session: Session) -> Actor | None:
    request.state.cfg = await sitecfg.get_all(session)  # название и цвета — на каждой странице
    return await webauth.actor_for_session(session, request.cookies.get(COOKIE))


Viewer = Annotated[Actor | None, Depends(current_actor)]


def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    cfg = ctx.setdefault("cfg", getattr(request.state, "cfg", None) or sitecfg.DEFAULTS)
    texts = sitecfg.texts_for(cfg)
    ctx["t"] = lambda key: texts.get(key, key)
    ctx["js_texts"] = {k: texts[k] for k in sitecfg.JS_TEXTS}
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def need_login(request: Request) -> HTMLResponse:
    return render(request, "login_needed.html", status=401)


# ---------- вход ----------


@app.get("/login/{token}", response_class=HTMLResponse)
async def login_form(token: str, request: Request, session: Session):
    # GET ничего не гасит: превью ссылок в мессенджерах не должны «съесть» вход
    emp = await webauth.login_token_valid(session, token)
    return render(request, "login.html", status=200 if emp else 410, token=token, employee=emp)


@app.post("/login/{token}")
async def login(token: str, session: Session):
    sess = await webauth.exchange_login_token(session, token)
    if sess is None:
        return RedirectResponse(f"/login/{token}", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, sess, max_age=int(webauth.SESSION_TTL.total_seconds()), httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout(request: Request, session: Session):
    await webauth.logout(session, request.cookies.get(COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------- страницы ----------


async def _menu(session: AsyncSession, actor: Actor):
    return await site.list_pages(session, actor, menu_only=True)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, session: Session, actor: Viewer):
    if actor is None:
        return need_login(request)
    menu = await _menu(session, actor)
    if menu:
        q = request.url.query
        return RedirectResponse(f"/p/{menu[0].slug}" + (f"?{q}" if q else ""), status_code=303)
    return render(request, "empty.html", actor=actor, menu=[], current=None)


@app.get("/p/{slug}", response_class=HTMLResponse)
async def page(slug: str, request: Request, session: Session, actor: Viewer,
               company: str | None = None, period: str | None = None,
               date_from: Annotated[str | None, Query(alias="from")] = None,
               date_to: Annotated[str | None, Query(alias="to")] = None):
    if actor is None:
        return need_login(request)
    menu = await _menu(session, actor)
    try:
        pg = await site.get_page(session, actor, slug)
    except NotFound:
        return render(request, "not_found.html", status=404, actor=actor, menu=menu, current=None)

    companies = await B.visible_companies(session, actor)
    total = len(await refs.list_companies(session))
    holding_label = sitecfg.texts_for(request.state.cfg)["holding"]
    options = B.company_options(companies, total, holding_label)
    cfg = request.state.cfg
    codes = [c for c, _ in options]
    if company not in codes:
        default = cfg["default_company"] or "holding"
        company = default if default in codes else (codes[0] if codes else "holding")
    # «Свои даты»: from/to из формы; иначе период из ссылки или по умолчанию
    try:
        if date_from and date_to and period in (None, "", "custom"):
            per = Period.days(date_from, date_to)
            period = per.spec
        else:
            period = period or cfg["default_period"]
            per = Period.parse(period)
    except ValidationError:
        period = cfg["default_period"]
        per = Period.parse(period)
    if period not in dict(B.PERIODS):
        period = per.spec  # конкретный период держим в ссылках точно
    ctx = B.ViewContext(actor, company, per, period, companies)

    try:
        fresh = await sync.freshness(session, actor, company, pg.department)
    except ServiceError:
        fresh = None
    views = await B.build(session, ctx, pg.blocks)
    return render(request, "page.html", actor=actor, menu=menu, current=pg, blocks=views, fresh=fresh,
                  company=company, company_options=options, period=period, periods=B.PERIODS,
                  custom=period not in dict(B.PERIODS), date_from=per.date_from.isoformat(),
                  date_to=per.date_to.isoformat(), partial=per.is_partial,
                  period_label=ctx.period.label, company_name=dict(options).get(company, holding_label if company == "holding" else ""),
                  no_companies=not total)


# ---------- канбан ----------


@app.post("/api/kanban/move")
async def kanban_move(request: Request, session: Session, actor: Viewer,
                      x_cg: Annotated[str | None, Header()] = None):
    if actor is None:
        return JSONResponse({"error": "Нужно войти заново"}, status_code=401)
    if x_cg != "1":  # защита от отправки с чужих сайтов
        return JSONResponse({"error": "bad request"}, status_code=400)
    data = await request.json()
    try:
        card = await kanban.get_card(session, actor, int(data["card_id"]))
        status = CardStatus(data["status"])
        comp = await session.get(Company, card.company_id)
        dept = await session.get(Department, card.department_id)
        board = await kanban.board(session, actor, comp.code, dept.code)
        column = [c.id for c in board[status] if c.id != card.id]
        # after_ids — карточки ниже места, куда бросили (на экране холдинга там бывают другие компании)
        after = [int(i) for i in data.get("after_ids") or []]
        position = next((column.index(i) for i in after if i in column), None)
        await changes.apply(session, actor, "kanban.move_card",
                            {"card_id": card.id, "status": status.value, "position": position})
    except PermissionDenied as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ServiceError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"error": "Неверный запрос"}, status_code=400)
    return {"ok": True}


# ---------- передача ключей (значение пишется в .env, боту не показывается) ----------


async def _secret_page(request, session, actor, public_id, **extra):
    req = await secrets.get_request(session, actor, public_id)
    from app.timeutil import utcnow

    return render(request, "secret.html", actor=actor, menu=await _menu(session, actor), current=None, req=req,
                  expired=req.expires_at < utcnow(), defaults=secrets.form_defaults(req), **extra)


@app.get("/secret/{public_id}", response_class=HTMLResponse)
async def secret_form(public_id: str, request: Request, session: Session, actor: Viewer):
    if actor is None:
        return need_login(request)
    try:
        return await _secret_page(request, session, actor, public_id)
    except (NotFound, PermissionDenied):
        return render(request, "not_found.html", status=404, actor=actor, menu=await _menu(session, actor), current=None)


@app.post("/secret/{public_id}", response_class=HTMLResponse)
async def secret_submit(public_id: str, request: Request, session: Session, actor: Viewer):
    if actor is None:
        return need_login(request)
    form = await request.form()
    value = form.get("value") or ""
    upload = form.get("file")
    if upload is not None and hasattr(upload, "read"):
        raw = await upload.read()
        if raw:
            value = raw[:secrets.MAX_VALUE + 1].decode("utf-8", errors="replace")
    fields = {k: str(form.get(k) or "") for k in ("url", "user", "password")}
    try:
        req = await secrets.fulfill(session, actor, public_id, str(value), fields=fields)
    except (NotFound, PermissionDenied):
        return render(request, "not_found.html", status=404, actor=actor, menu=await _menu(session, actor), current=None)
    except ValidationError as e:
        return await _secret_page(request, session, actor, public_id, error=str(e),
                                  form={"url": fields["url"], "user": fields["user"]})  # пароль не возвращаем
    import asyncio

    asyncio.get_running_loop().create_task(secrets.notify(req))  # бот продолжит задачу
    return await _secret_page(request, session, actor, public_id, done=True)


@app.get("/api/sync/progress")
async def sync_progress(actor: Viewer):
    if actor is None:
        return JSONResponse({"error": "Нужно войти заново"}, status_code=401)
    p = onec.progress()
    if not actor.is_owner:  # сотрудникам — только сам факт загрузки, без названий источников
        p = {k: p[k] for k in ("running", "percent", "done", "total", "run_id", "changed", "finished_at")}
    return p


@app.get("/healthz")
async def healthz() -> Response:
    return Response("ok", media_type="text/plain")
