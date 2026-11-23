"""Ключи и токены внешних сервисов. Хранятся только в .env.

Бот значения не видит: он создаёт запрос (request), владелец открывает ссылку на сайте
и вводит ключ, сервер пишет его в .env. Боту приходит только «ключ получен».
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets as pysecrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SecretRequest
from app.services import changelog
from app.services.access import require_owner
from app.services.context import Actor
from app.services.errors import NotFound, ValidationError
from app.timeutil import utcnow

ENV_PATH: Path = settings.secrets_file   # тесты подменяют
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# Эти ключи бот не трогает: доступ к Telegram, Claude, базе, сайту и 1С
PROTECTED = re.compile(r"^(OWNER_TELEGRAM_ID|ANTHROPIC_API_KEY|DATABASE_URL|BOT_TOKEN_.*|SITE_.*|ONEC_.*|MODEL_.*|"
                       r"MAIN_EFFORT|CLAUDE_.*|TIMEZONE|SYNC_STALE_HOURS)$")
REQUEST_TTL = timedelta(hours=24)
B64 = "b64:"
MAX_VALUE = 20_000

# Кто хочет знать, что ключ получен (главный бот продолжает задачу). Вызывается в фоне.
Listener = Callable[[str, str], Awaitable[None]]
_listeners: list[Listener] = []


def on_fulfilled(fn: Listener) -> None:
    _listeners.append(fn)


def check_key(key: str) -> str:
    key = (key or "").strip().upper()
    if not KEY_RE.match(key):
        raise ValidationError("Имя ключа — латиница в верхнем регистре, цифры и _: AMO_TOKEN, GSHEETS_KEY")
    if PROTECTED.match(key):
        raise ValidationError(f"«{key}» — служебная настройка, её бот не меняет")
    return key


# ---------- .env ----------


def _encode(value: str) -> str:
    # одна строка без кавычек и пробелов — как есть; иначе base64, чтобы .env не сломался
    if re.fullmatch(r"[A-Za-z0-9_\-.:/@+=~]*", value):
        return value
    return B64 + base64.b64encode(value.encode()).decode()


def get(key: str) -> str | None:
    """Значение ключа (для вызовов сервисов; наружу не отдаётся)."""
    v = os.environ.get(key)
    if not v and ENV_PATH.exists():
        v = dotenv_values(ENV_PATH).get(key)
    if not v:
        return None
    if v.startswith(B64):
        return base64.b64decode(v[len(B64):]).decode()
    return v


def names() -> list[str]:
    """Имена ключей в .env, которые бот может использовать (служебные не показываем)."""
    if not ENV_PATH.exists():
        return []
    return sorted(k for k, v in dotenv_values(ENV_PATH).items() if v and not PROTECTED.match(k))


def _write(key: str, value: str) -> None:
    line = f"{key}={_encode(value)}"
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, done = [], False
    for ln in lines:
        if re.match(rf"^\s*(export\s+)?{re.escape(key)}\s*=", ln):
            if not done:
                out.append(line)
                done = True
            continue
        out.append(ln)
    if not done:
        if out and out[-1].strip():
            out.append("")
        out.append(line)
    tmp = ENV_PATH.with_name(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_PATH)


# ---------- запросы ----------


async def create_request(session: AsyncSession, actor: Actor, *, key: str, title: str,
                         instructions: str | None = None, kind: str = "text",
                         _allow_onec: bool = False) -> SecretRequest:
    require_owner(actor, "запрашивать ключи")
    if _allow_onec:  # подключения 1С — только через create_onec_request
        key = key.strip().upper()
    else:
        key = check_key(key)
    if kind not in ("text", "json") and not (_allow_onec and kind == "onec"):
        raise ValidationError("kind: text (строка) или json (файл ключа, например Google)")
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise ValidationError("Нужно короткое название: что именно запрашиваем")
    now = utcnow()
    existing = await session.scalar(select(SecretRequest).where(
        SecretRequest.key == key, SecretRequest.fulfilled_at.is_(None), SecretRequest.expires_at > now))
    if existing:
        existing.title, existing.kind = title, kind
        if instructions:
            existing.instructions = instructions
        existing.expires_at = now + REQUEST_TTL
        await session.commit()
        return existing
    req = SecretRequest(public_id=pysecrets.token_urlsafe(16), key=key, title=title, instructions=instructions,
                        kind=kind, expires_at=now + REQUEST_TTL)
    session.add(req)
    await changelog.log(session, actor, entity="secret", entity_id=None, action="request",
                        summary=f"Запрошен ключ {key}: {title}")
    await session.commit()
    return req


ONEC_NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,30}$")


def check_onec_url(url: str) -> str:
    from urllib.parse import urlsplit

    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError("Адрес должен начинаться с http:// или https://, например "
                              "http://сервер/база/odata/standard.odata")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise ValidationError("В адресе не должно быть логина, пароля и параметров после «?» — только путь к OData")
    if len(url) > 500 or re.search(r"\s", url):
        raise ValidationError("Недопустимый адрес")
    return url


async def create_onec_request(session: AsyncSession, actor: Actor, *, name: str, url: str | None = None,
                              user: str | None = None, instructions: str | None = None) -> SecretRequest:
    """Запросить у владельца новую базу 1С. Он вводит адрес, логин и пароль на сайте; сервер проверяет
    связь и пишет ONEC_<ИМЯ>_URL/_USER/_PASSWORD в .env. Существующие подключения не трогаем."""
    from app.integrations import onec as onec_api

    require_owner(actor, "подключать базы 1С")
    name = (name or "").strip().lower()
    if not ONEC_NAME_RE.match(name):
        raise ValidationError("Имя подключения — латиница и цифры, со строчной буквы: velse, ut2")
    if name in onec_api.load_connections():
        raise ValidationError(f"Подключение «{name}» уже есть — его не меняю. Выбери другое имя")
    defaults = {}
    if url:
        defaults["url"] = check_onec_url(url)
    if user:
        defaults["user"] = user.strip()[:100]
    req = await create_request(session, actor, key=f"ONEC_{name.upper()}", title=f"Подключение к 1С «{name}»",
                               instructions=instructions, kind="onec", _allow_onec=True)
    req.defaults = json.dumps(defaults, ensure_ascii=False) if defaults else None
    await session.commit()
    return req


def form_defaults(req: SecretRequest) -> dict:
    try:
        return json.loads(req.defaults) if req.defaults else {}
    except ValueError:
        return {}


async def _fulfill_onec(req: SecretRequest, fields: dict) -> str:
    from app.integrations import onec as onec_api

    name = req.key.removeprefix("ONEC_").lower()
    if name in onec_api.load_connections():
        raise ValidationError(f"Подключение «{name}» уже есть в .env — его не меняю")
    url = check_onec_url(fields.get("url", ""))
    user = (fields.get("user") or "").strip()
    password = fields.get("password") or ""
    if not user:
        raise ValidationError("Укажите логин пользователя 1С")
    if len(user) > 100 or len(password) > 500 or "\n" in user + password:
        raise ValidationError("Слишком длинный логин или пароль")
    conn = onec_api.Connection(name=name, url=url, user=user, password=password)
    try:  # сначала проверяем связь — неверный пароль не должен попасть в .env
        async with onec_api.Client(conn, timeout=20) as cl:
            objects = await cl.ping()
    except onec_api.OneCError as e:
        raise ValidationError(f"Не удалось подключиться: {e}. Проверьте адрес, логин и пароль") from None
    _write(f"{req.key}_URL", url)
    _write(f"{req.key}_USER", user)
    _write(f"{req.key}_PASSWORD", password)
    return f"{objects} объектов OData"


async def get_request(session: AsyncSession, actor: Actor, public_id: str) -> SecretRequest:
    require_owner(actor, "передавать ключи")
    req = await session.scalar(select(SecretRequest).where(SecretRequest.public_id == public_id))
    if req is None:
        raise NotFound("Запрос не найден")
    return req


def _normalize(kind: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationError("Пустое значение")
    if len(value) > MAX_VALUE:
        raise ValidationError("Слишком длинное значение")
    if kind == "json":
        try:
            data = json.loads(value)
        except ValueError:
            raise ValidationError("Это не JSON. Загрузите файл ключа целиком, как его скачали") from None
        if not isinstance(data, dict):
            raise ValidationError("Ожидался JSON-объект { … }")
        if data.get("type") == "service_account" and not (data.get("private_key") and data.get("client_email")):
            raise ValidationError("В файле сервисного аккаунта нет private_key или client_email")
        value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return value


async def fulfill(session: AsyncSession, actor: Actor, public_id: str, value: str,
                  fields: dict | None = None) -> SecretRequest:
    req = await get_request(session, actor, public_id)
    if req.fulfilled_at is not None:
        raise ValidationError("Этот ключ уже передан")
    if req.expires_at < utcnow():
        raise ValidationError("Запрос устарел — попросите бота запросить ключ ещё раз")
    if req.kind == "onec":
        checked = await _fulfill_onec(req, fields or {})
        summary = f"Через сайт добавлено подключение 1С {req.key.removeprefix('ONEC_').lower()} (проверено: {checked})"
    else:
        _write(req.key, _normalize(req.kind, value))
        summary = f"Через сайт сохранён ключ {req.key} ({req.title})"
    req.fulfilled_at = utcnow()
    await changelog.log(session, actor, entity="secret", entity_id=None, action="set",
                        summary=summary)  # само значение не пишем
    await session.commit()
    return req


async def notify(req: SecretRequest) -> None:
    import logging

    for fn in list(_listeners):
        try:
            await fn(req.key, req.title)
        except Exception:  # noqa: BLE001 — сайт не должен падать из-за бота
            logging.getLogger(__name__).exception("secret listener failed")


async def pending_requests(session: AsyncSession, actor: Actor) -> list[SecretRequest]:
    require_owner(actor, "смотреть запросы ключей")
    return list(await session.scalars(select(SecretRequest).where(
        SecretRequest.fulfilled_at.is_(None), SecretRequest.expires_at > utcnow())))


def redact(text: str, keys: list[str]) -> str:
    """Убрать значения ключей из текста (ответы сервисов иногда эхом возвращают токен)."""
    for k in keys:
        v = get(k)
        if v and len(v) >= 6:
            text = text.replace(v, "[скрыто]")
    return text
