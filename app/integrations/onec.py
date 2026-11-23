"""Чтение из 1С через стандартный интерфейс OData. Только GET: в 1С никогда не пишем.

Подключения хранятся в .env (пароли — только там), по три строки на базу. Владелец вводит
их на сайте по ссылке от бота (secrets.create_onec_request) или вписывает сам:

    ONEC_UT_URL=http://server/ut/odata/standard.odata
    ONEC_UT_USER=bot_readonly
    ONEC_UT_PASSWORD=...

Необязательно: ONEC_UT_COMPANY=alpha(какой компании база), ONEC_UT_VERIFY_SSL=false
(самоподписанный сертификат). .env перечитывается при каждом обращении — после
добавления базы перезапуск не нужен.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote

import httpx
from dotenv import dotenv_values

from app.config import settings
from app.services.errors import ServiceError, ValidationError
from app.services.periods import add_months

_URL_KEY = re.compile(r"^ONEC_([A-Z0-9]+)_URL$")
PAGE = 5000
MAX_ROWS = 200_000
METADATA_TTL = 3600

# Для тестов: подменяется на httpx.MockTransport
TRANSPORT: httpx.AsyncBaseTransport | None = None


class OneCError(ServiceError):
    """Ошибка обращения к 1С. Текст понятен человеку."""


@dataclass(frozen=True)
class Connection:
    name: str
    url: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    company: str | None = None
    verify_ssl: bool = True

    @property
    def safe_url(self) -> str:
        return re.sub(r"//[^/@]*@", "//", self.url)


def _env_path():
    from app.services import secrets  # там же путь, который подменяют тесты

    return secrets.ENV_PATH


def _decode(v: str) -> str:
    # значения с пробелами и спецсимволами сайт пишет как b64:… (см. services/secrets.py)
    if v.startswith("b64:"):
        import base64

        return base64.b64decode(v[4:]).decode()
    return v


def _env() -> dict[str, str]:
    path = _env_path()
    env = {k: _decode(v) for k, v in dotenv_values(path).items() if v} if path.exists() else {}
    env.update({k: v for k, v in os.environ.items() if k.startswith("ONEC_") and v})
    return env


def load_connections(env: dict[str, str] | None = None) -> dict[str, Connection]:
    env = _env() if env is None else env
    out = {}
    for key, url in env.items():
        m = _URL_KEY.match(key)
        if not m or not url:
            continue
        n = m.group(1)
        out[n.lower()] = Connection(
            name=n.lower(),
            url=url.strip().rstrip("/"),
            user=env.get(f"ONEC_{n}_USER") or None,
            password=env.get(f"ONEC_{n}_PASSWORD") or None,
            company=(env.get(f"ONEC_{n}_COMPANY") or "").strip().lower() or None,
            verify_ssl=(env.get(f"ONEC_{n}_VERIFY_SSL") or "true").strip().lower() not in ("0", "false", "no"),
        )
    return dict(sorted(out.items()))


def get_connection(name: str) -> Connection:
    conns = load_connections()
    conn = conns.get((name or "").strip().lower())
    if conn is None:
        known = ", ".join(conns) or "ни одного"
        raise ValidationError(
            f"Подключение к 1С «{name}» не настроено (настроены: {known}). Запроси его у владельца "
            "через onec_request_connection — он введёт адрес, логин и пароль на сайте")
    return conn


# ---------- адрес запроса ----------

_BAD_PATH = re.compile(r"[?#&\\]|\.\.|^\$|^/")
_BAD_FILTER = re.compile(r"[#&]")


def check_entity(entity: str) -> str:
    entity = (entity or "").strip()
    if not entity or _BAD_PATH.search(entity) or len(entity) > 500:
        raise ValidationError(f"Недопустимое имя объекта 1С: «{entity}»")
    return entity


def check_filter(flt: str | None) -> str | None:
    flt = (flt or "").strip() or None
    if flt and (_BAD_FILTER.search(flt) or len(flt) > 2000):
        raise ValidationError("Недопустимые символы в отборе ($filter)")
    return flt


def _q(v: str) -> str:
    # пробелы — %20 (1С не всегда понимает «+»), кириллица — в UTF-8
    return quote(v, safe="'(),=:$*/-_.")


def _path(entity: str) -> str:
    return quote(entity, safe="/(),='-_.:$*")


def _parse_error(r: httpx.Response) -> str | None:
    try:
        err = r.json().get("odata.error") or r.json().get("error") or {}
        msg = err.get("message")
        return (msg.get("value") if isinstance(msg, dict) else msg) or None
    except Exception:  # noqa: BLE001 — ответ не JSON
        text = r.text.strip()
        return text[:300] if text and "<html" not in text[:200].lower() else None


class Client:
    """Клиент одной базы. Использовать как async with."""

    _metadata_cache: dict[str, tuple[float, dict[str, list[tuple[str, str]]]]] = {}

    def __init__(self, conn: Connection, timeout: float | None = None):
        self.conn = conn
        auth = httpx.BasicAuth(conn.user, conn.password or "") if conn.user else None
        self.http = httpx.AsyncClient(
            auth=auth, verify=conn.verify_ssl, timeout=timeout or settings.onec_timeout,
            transport=TRANSPORT, headers={"Accept": "application/json"}, follow_redirects=True)

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, str | int | None] | None = None,
                   what: str | None = None) -> httpx.Response:
        qs = "&".join(f"{k}={_q(str(v))}" for k, v in (params or {}).items() if v is not None)
        url = f"{self.conn.url}/{path}" + (f"?{qs}" if qs else "")
        try:
            r = await self.http.request("GET", url)  # только чтение
        except httpx.TimeoutException:
            raise OneCError(f"1С ({self.conn.name}) не ответила вовремя. Сузьте запрос (отбор, период) "
                            "или увеличьте ONEC_TIMEOUT") from None
        except httpx.HTTPError as e:
            raise OneCError(f"Нет связи с 1С ({self.conn.name}, {self.conn.safe_url}): {e.__class__.__name__}") from None
        if r.status_code == 401:
            n = self.conn.name.upper()
            raise OneCError(f"1С ({self.conn.name}) не пустила: проверьте ONEC_{n}_USER и ONEC_{n}_PASSWORD в .env")
        if r.status_code == 403:
            raise OneCError(f"У пользователя 1С нет прав на чтение{f' «{what}»' if what else ''}")
        if r.status_code == 404:
            detail = _parse_error(r)
            raise OneCError(
                f"В 1С ({self.conn.name}) не найдено{f' «{what}»' if what else ''}. Либо имя написано неверно, "
                "либо объект не включён в состав OData (в 1С: «Настройка состава стандартного интерфейса OData»)"
                + (f". Ответ 1С: {detail}" if detail else ""))
        if r.status_code >= 400:
            detail = _parse_error(r)
            raise OneCError(f"1С ({self.conn.name}) вернула ошибку {r.status_code}" + (f": {detail}" if detail else ""))
        return r

    def _json(self, r: httpx.Response) -> dict:
        try:
            return r.json()
        except ValueError:
            raise OneCError(f"1С ({self.conn.name}) ответила не в формате JSON — проверьте, что адрес "
                            "заканчивается на /odata/standard.odata") from None

    # ---------- справочная информация ----------

    async def entity_sets(self) -> list[str]:
        data = self._json(await self._get("", {"$format": "json"}))
        return [v.get("name") or v.get("url") for v in data.get("value", [])]

    async def metadata(self) -> dict[str, list[tuple[str, str]]]:
        """Типы объектов из $metadata: имя → [(поле, тип)]. Кешируется на час."""
        cached = self._metadata_cache.get(self.conn.url)
        if cached and time.monotonic() - cached[0] < METADATA_TTL:
            return cached[1]
        r = await self._get("$metadata", what="$metadata")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise OneCError("Не удалось разобрать $metadata 1С") from None
        types: dict[str, list[tuple[str, str]]] = {}
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] != "EntityType":
                continue
            props = []
            for p in el:
                tag = p.tag.rsplit("}", 1)[-1]
                if tag == "Property":
                    props.append((p.get("Name"), (p.get("Type") or "").replace("Edm.", "")))
                elif tag == "NavigationProperty":
                    props.append((p.get("Name"), "ссылка"))
            types[el.get("Name")] = props
        self._metadata_cache[self.conn.url] = (time.monotonic(), types)
        return types

    async def describe(self, entity: str) -> dict:
        entity = check_entity(entity)
        base = entity.split("(", 1)[0]
        types = await self.metadata()
        candidates = [base.replace("/", "_"), base.replace("/", "_").rstrip("s")]
        fields = next((types[c] for c in candidates if c in types), None)
        virtual = sorted(t[len(base) + 1:] for t in types if "/" not in base and t.startswith(base + "_")
                         and t[len(base) + 1:] in ("Turnover", "Balance", "BalanceAndTurnover", "SliceLast",
                                                   "SliceFirst", "RecordType", "DrCrTurnover", "ExtDimensions"))
        out: dict = {"entity": entity}
        if fields is None:
            # нет в $metadata (бывает у виртуальных таблиц) — смотрим поля по первой строке
            rows = await self.rows(entity, top=1)
            if not rows:
                raise OneCError(f"Объект «{entity}» есть, но в нём нет строк — поля определить не по чему")
            fields = [(k, type(v).__name__) for k, v in rows[0].items() if not k.startswith("odata.")]
        out["fields"] = [{"name": n, "type": t} for n, t in fields]
        if virtual:
            names = {"Turnover": "Turnovers", "BalanceAndTurnover": "BalanceAndTurnovers"}
            out["virtual_tables"] = [f"{base}/{names.get(v, v)}" for v in virtual
                                     if v in ("Turnover", "Balance", "BalanceAndTurnover", "SliceLast", "SliceFirst")]
        return out

    # ---------- данные ----------

    async def rows(self, entity: str, *, select: str | None = None, filter: str | None = None,
                   top: int | None = None, skip: int | None = None, orderby: str | None = None,
                   expand: str | None = None) -> list[dict]:
        entity = check_entity(entity)
        params = {"$format": "json", "$select": select, "$filter": check_filter(filter), "$top": top,
                  "$skip": skip or None, "$orderby": orderby, "$expand": expand}
        data = self._json(await self._get(_path(entity), params, what=entity.split("(", 1)[0]))
        value = data.get("value")
        if value is None:  # один объект, а не коллекция
            return [data]
        return value

    async def count(self, entity: str, filter: str | None = None) -> int:
        entity = check_entity(entity)
        try:
            r = await self._get(_path(entity) + "/$count", {"$filter": check_filter(filter)},
                                what=entity.split("(", 1)[0])
            return int(r.text.strip())
        except (OneCError, ValueError):
            n = 0
            async for page in self.pages(entity, select=None, filter=filter):
                n += len(page)
            return n

    async def pages(self, entity: str, *, select: str | None, filter: str | None):
        skip = 0
        while True:
            page = await self.rows(entity, select=select, filter=filter, top=PAGE, skip=skip)
            yield page
            skip += len(page)
            if len(page) < PAGE:
                return
            if skip >= MAX_ROWS:
                raise OneCError(f"Больше {MAX_ROWS} строк — сузьте отбор или используйте виртуальную таблицу "
                                "Turnovers/Balance, она уже сгруппирована")

    async def ping(self) -> int:
        return len(await self.entity_sets())


# ---------- расчёт показателя за месяц ----------

PERIOD_MODES = {
    "turnovers": "обороты за месяц (виртуальная таблица …/Turnovers)",
    "balance": "остаток на конец месяца (…/Balance или …/SliceLast)",
    "date_field": "строки с датой внутри месяца (документы)",
    "none": "без отбора по периоду",
}
AGGREGATIONS = {"sum": "сумма поля", "count": "количество строк"}


@dataclass
class Source:
    """Откуда в 1С берётся значение показателя."""

    entity: str
    field: str | None = None
    aggregation: str = "sum"
    filter: str | None = None
    period_mode: str = "turnovers"
    date_field: str | None = None
    multiplier: float = 1.0

    def validate(self) -> Source:
        self.entity = check_entity(self.entity)
        self.filter = check_filter(self.filter)
        self.field = (self.field or "").strip() or None
        self.date_field = (self.date_field or "").strip() or None
        if self.aggregation not in AGGREGATIONS:
            raise ValidationError("Способ расчёта: sum (сумма поля) или count (количество строк)")
        if self.aggregation == "sum" and not self.field:
            raise ValidationError("Для суммы нужно поле (field) — числовой реквизит или ресурс")
        if self.field and not re.fullmatch(r"[\w]+", self.field):
            raise ValidationError(f"Недопустимое имя поля «{self.field}»")
        if self.period_mode not in PERIOD_MODES:
            raise ValidationError("Период: turnovers, balance, date_field или none")
        base = self.entity.split("(", 1)[0]
        if self.period_mode == "turnovers" and not base.endswith(("/Turnovers", "/BalanceAndTurnovers")):
            raise ValidationError("Для оборотов за месяц нужна виртуальная таблица: имя вида "
                                  "AccumulationRegister_Продажи/Turnovers")
        if self.period_mode == "balance" and not base.endswith(("/Balance", "/SliceLast", "/BalanceAndTurnovers")):
            raise ValidationError("Для остатка на конец месяца нужна виртуальная таблица: имя вида "
                                  "AccumulationRegister_ТоварыНаСкладах/Balance")
        if self.period_mode == "date_field":
            if not self.date_field or not re.fullmatch(r"[\w]+", self.date_field):
                raise ValidationError("Для отбора по дате нужно поле даты (date_field), обычно Date")
        try:
            self.multiplier = float(self.multiplier if self.multiplier is not None else 1)
        except (TypeError, ValueError):
            raise ValidationError("Множитель должен быть числом") from None
        if self.multiplier == 0:
            raise ValidationError("Множитель не может быть 0")
        return self

    def describe(self) -> str:
        what = f"сумма {self.field}" if self.aggregation == "sum" else "количество строк"
        s = f"{what} из {self.entity}"
        if self.period_mode == "date_field":
            s += f", по дате {self.date_field}"
        elif self.period_mode != "turnovers":
            s += f", {PERIOD_MODES[self.period_mode]}"
        if self.filter:
            s += f", отбор: {self.filter}"
        if self.multiplier != 1:
            s += f", × {self.multiplier:g}"
        return s


def month_bounds(month: date, end: date | None = None) -> dict[str, str]:
    """Границы для запроса: с month (первого дня) по end включительно (по умолчанию — конец месяца)."""
    if end is None:
        end = add_months(month, 1) - timedelta(days=1)
    nxt = end + timedelta(days=1)
    return {"start": f"{month:%Y-%m-%d}T00:00:00", "end": f"{end:%Y-%m-%d}T23:59:59",
            "next": f"{nxt:%Y-%m-%d}T00:00:00"}


def _fill(text: str | None, b: dict[str, str]) -> str | None:
    if text is None:
        return None
    for k, v in b.items():
        text = text.replace("{" + k + "}", v)
    return text


def build_request(src: Source, month: date, end: date | None = None) -> tuple[str, str | None]:
    """→ (объект с параметрами виртуальной таблицы, $filter) для месяца или отрезка дней month..end."""
    b = month_bounds(month, end)
    entity, flt = _fill(src.entity, b), _fill(src.filter, b)
    if "(" not in entity:
        if src.period_mode == "turnovers":
            entity += f"(StartPeriod=datetime'{b['start']}',EndPeriod=datetime'{b['end']}')"
        elif src.period_mode == "balance":
            entity += f"(Period=datetime'{b['next']}')"
    if src.period_mode == "date_field":
        cond = f"{src.date_field} ge datetime'{b['start']}' and {src.date_field} le datetime'{b['end']}'"
        flt = f"({flt}) and {cond}" if flt else cond
    return entity, flt


@dataclass
class Computed:
    value: float
    rows: int
    sample: list[dict]
    request: str


def _number(v, fld: str) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        raise OneCError(f"Поле «{fld}» логическое, а не число")
    if isinstance(v, int | float):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        raise OneCError(f"Поле «{fld}» не числовое (пример значения: {str(v)[:50]})") from None


async def compute(client: Client, src: Source, month: date, end: date | None = None) -> Computed:
    entity, flt = build_request(src, month, end)
    request = entity + (f" ? $filter={flt}" if flt else "")
    if src.aggregation == "count":
        n = await client.count(entity, flt)
        return Computed(n * src.multiplier, n, [], request)
    total, n, sample = 0.0, 0, []
    async for page in client.pages(entity, select=src.field, filter=flt):
        for row in page:
            if src.field not in row:
                raise OneCError(f"В «{entity.split('(', 1)[0]}» нет поля «{src.field}». "
                                f"Есть: {', '.join(k for k in row if not k.startswith('odata.'))[:500]}")
            total += _number(row[src.field], src.field)
            n += 1
            if len(sample) < 3:
                sample.append(row)
    return Computed(total * src.multiplier, n, sample, request)
