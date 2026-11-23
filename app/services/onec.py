"""1С: просмотр баз, источники показателей и загрузка фактов. Всё — только владельцу.

В 1С только читаем (app/integrations/onec.py делает лишь GET). Пишем — в свою БД:
настройки источников (onec_sources) и факты (metric_values.fact, source='1c').
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import onec as api
from app.models import Aggregation, Company, Department, MetricDefinition, MetricValue, OneCSource, SyncState, SyncStatus
from app.services import changelog, refs
from app.services.access import require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import NotFound, ValidationError
from app.services.format import fmt_value, month_label
from app.services.periods import Period, add_months, month_start, parse_month, today_local
from app.timeutil import utcnow

log = logging.getLogger(__name__)

SOURCE = "1c"
MAX_CALLS = 600          # запросов к 1С за одну загрузку
_FIELDS = ["connection", "entity", "field", "aggregation", "filter", "period_mode", "date_field",
           "multiplier", "note", "is_active"]
_LABELS = {"connection": "Подключение", "entity": "Объект 1С", "field": "Поле", "aggregation": "Расчёт",
           "filter": "Отбор", "period_mode": "Период", "date_field": "Поле даты", "multiplier": "Множитель",
           "note": "Заметка", "is_active": "Загружать"}
_sync_lock = asyncio.Lock()
_wake: asyncio.Event | None = None


@dataclass
class Progress:
    """Ход текущей загрузки из 1С — для полосы на сайте и сообщения в Telegram. Живёт в памяти процесса."""

    running: bool = False
    total: int = 0
    done: int = 0
    current: str | None = None
    period: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: str | None = None
    changed: int = 0
    errors: int = 0
    run_id: int = 0


PROGRESS = Progress()


def progress() -> dict:
    d = asdict(PROGRESS)
    d["percent"] = round(PROGRESS.done / PROGRESS.total * 100) if PROGRESS.total else (100 if not PROGRESS.running else 0)
    for k in ("started_at", "finished_at"):
        d[k] = d[k].isoformat(timespec="seconds") + "Z" if d[k] else None
    return d


def wake_event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def wake() -> None:
    """Разбудить фоновую загрузку: новый источник подтянется сразу, а не через минуту."""
    wake_event().set()


def _own(actor: Actor) -> None:
    require_owner(actor, "работать с 1С")


# ---------- просмотр баз ----------


async def connections(actor: Actor) -> list[dict]:
    _own(actor)
    out = []
    for c in api.load_connections().values():
        item = {"name": c.name, "url": c.safe_url, "user": c.user, "company": c.company}
        try:
            async with api.Client(c, timeout=15) as cl:
                item["objects"] = await cl.ping()
                item["ok"] = True
        except api.OneCError as e:
            item["ok"], item["error"] = False, str(e)
        out.append(item)
    return out


async def find_objects(actor: Actor, connection: str, search: str | None = None, kind: str | None = None,
                       limit: int = 200) -> dict:
    _own(actor)
    async with api.Client(api.get_connection(connection)) as cl:
        names = await cl.entity_sets()
    total = len(names)
    if kind:
        names = [n for n in names if n.lower().startswith(kind.lower())]
    if search:
        words = search.lower().split()
        names = [n for n in names if all(w in n.lower() for w in words)]
    return {"total_in_base": total, "found": len(names), "objects": names[:limit],
            **({"note": f"показаны первые {limit} — уточните поиск"} if len(names) > limit else {})}


async def describe(actor: Actor, connection: str, entity: str) -> dict:
    _own(actor)
    async with api.Client(api.get_connection(connection)) as cl:
        return await cl.describe(entity)


def _short(v, n: int = 200):
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "…"
    if isinstance(v, list):
        return f"[{len(v)} строк]"
    if isinstance(v, dict):
        return {k: _short(x, n) for k, x in list(v.items())[:30]}
    return v


async def query(actor: Actor, connection: str, entity: str, select: str | None = None, filter: str | None = None,
                top: int = 10, orderby: str | None = None, expand: str | None = None) -> dict:
    _own(actor)
    top = max(1, min(int(top or 10), 50))
    async with api.Client(api.get_connection(connection)) as cl:
        rows = await cl.rows(entity, select=select, filter=filter, top=top, orderby=orderby, expand=expand)
    return {"rows": [{k: _short(v) for k, v in r.items() if not k.startswith("odata.")} for r in rows],
            "shown": len(rows), **({"note": f"показаны первые {top}"} if len(rows) == top else {})}


def _source(params: dict) -> api.Source:
    return api.Source(**{k: params[k] for k in ("entity", "field", "aggregation", "filter", "period_mode",
                                                 "date_field", "multiplier") if k in params}).validate()


async def test_source(actor: Actor, connection: str, month: str | None = None, **params) -> dict:
    """Посчитать значение по описанию источника — без сохранения."""
    _own(actor)
    src = _source(params)
    m = parse_month(month) if month else month_start(today_local())
    async with api.Client(api.get_connection(connection)) as cl:
        res = await api.compute(cl, src, m)
    return {"month": month_label(m), "value": round(res.value, 2), "rows_used": res.rows,
            "request": res.request, "sample_rows": [{k: _short(v) for k, v in r.items()} for r in res.sample]}


# ---------- источники показателей ----------


async def _base_metric(session: AsyncSession, code: str) -> MetricDefinition:
    mdef = await refs.get_metric(session, code)
    if mdef.aggregation == Aggregation.DERIVED:
        raise ValidationError(f"«{mdef.name}» считается формулой ({mdef.formula}) — из 1С загружаются "
                              "исходные показатели")
    return mdef


async def _find(session: AsyncSession, metric_id: int, company_id: int) -> OneCSource | None:
    return await session.scalar(select(OneCSource).where(OneCSource.metric_id == metric_id,
                                                         OneCSource.company_id == company_id))


def _show(field: str, v) -> str | None:
    if v is None or v == "":
        return None if field not in ("is_active",) else "—"
    if field == "aggregation":
        return api.AGGREGATIONS.get(v, v)
    if field == "period_mode":
        return api.PERIOD_MODES.get(v, v)
    if field == "is_active":
        return "да" if v else "нет"
    if field == "multiplier":
        return f"{float(v):g}"
    return str(v)


async def list_sources(session: AsyncSession, actor: Actor) -> list[dict]:
    _own(actor)
    rows = await session.execute(
        select(OneCSource, MetricDefinition, Company)
        .join(MetricDefinition, MetricDefinition.id == OneCSource.metric_id)
        .join(Company, Company.id == OneCSource.company_id)
        .order_by(Company.sort_order, MetricDefinition.sort_order))
    out = []
    for s, m, c in rows:
        out.append({"metric": m.code, "metric_name": m.name, "company": c.code, "connection": s.connection,
                    "entity": s.entity, "field": s.field, "aggregation": s.aggregation, "filter": s.filter,
                    "period_mode": s.period_mode, "date_field": s.date_field, "multiplier": s.multiplier,
                    "note": s.note, "active": s.is_active,
                    "last_run": s.last_run_at.isoformat(timespec="minutes") if s.last_run_at else None,
                    "last_error": s.last_error})
    return out


@write_op("onec.save_source")
async def save_source(
    session: AsyncSession,
    actor: Actor,
    *,
    metric: str,
    company: str,
    connection: str,
    entity: str,
    field: str | None = None,
    aggregation: str = "sum",
    filter: str | None = None,
    period_mode: str = "turnovers",
    date_field: str | None = None,
    multiplier: float = 1.0,
    note: str | None = UNSET,
    is_active: bool = True,
    dry_run: bool = False,
) -> ChangePreview:
    _own(actor)
    mdef = await _base_metric(session, metric)
    comp = await refs.get_company(session, company)
    conn = api.get_connection(connection)
    src = api.Source(entity, field, aggregation, filter, period_mode, date_field, multiplier).validate()
    new = {"connection": conn.name, **{k: getattr(src, k) for k in ("entity", "field", "aggregation", "filter",
                                                                     "period_mode", "date_field", "multiplier")},
           "is_active": bool(is_active)}
    row = await _find(session, mdef.id, comp.id)
    if note is not UNSET:
        new["note"] = (note or "").strip() or None
    elif row is None:
        new["note"] = None
    old = changelog.snapshot(row, _FIELDS) if row else {}
    diff = [FieldChange(_LABELS[k], _show(k, old.get(k)) if row else None, _show(k, v) or "—")
            for k, v in new.items() if (row and old.get(k) != v) or (not row and v is not None)]
    if row and not diff:
        raise ValidationError("Источник уже настроен именно так — менять нечего")
    preview = ChangePreview(title=f"Источник в 1С: «{mdef.name}» · {comp.name}", changes=diff)
    if dry_run:
        return preview
    if row is None:
        row = OneCSource(metric_id=mdef.id, company_id=comp.id)
        session.add(row)
    for k, v in new.items():
        setattr(row, k, v)
    row.last_error = None
    row.last_run_at = None  # «новый»: фоновая загрузка сразу подтянет историю
    await session.flush()
    await changelog.log(session, actor, entity="onec_source", entity_id=row.id,
                        action="update" if old else "create", summary=preview.render(),
                        before=old or None, after=changelog.snapshot(row, _FIELDS))
    await session.commit()
    preview.entity_id = row.id
    wake()
    return preview


@write_op("onec.delete_source")
async def delete_source(session: AsyncSession, actor: Actor, *, metric: str, company: str,
                        dry_run: bool = False) -> ChangePreview:
    _own(actor)
    mdef = await refs.get_metric(session, metric)
    comp = await refs.get_company(session, company)
    row = await _find(session, mdef.id, comp.id)
    if row is None:
        raise NotFound(f"Для «{mdef.name}» · {comp.name} источник в 1С не настроен")
    src = api.Source(row.entity, row.field, row.aggregation, row.filter, row.period_mode, row.date_field,
                     row.multiplier)
    preview = ChangePreview(
        title=f"Удалить источник в 1С: «{mdef.name}» · {comp.name}",
        changes=[FieldChange("Источник", src.describe(), None),
                 FieldChange("Уже загруженные факты", None, "останутся как есть")])
    if dry_run:
        return preview
    before = changelog.snapshot(row, _FIELDS)
    await changelog.log(session, actor, entity="onec_source", entity_id=row.id, action="delete",
                        summary=preview.render(), before=before)
    await session.delete(row)
    await session.commit()
    return preview


# ---------- факт за отрезок дней прямо из 1С ----------

LIVE_TTL = 300
_live_cache: dict[tuple, tuple[float, float | Exception]] = {}


async def live_facts(session: AsyncSession, company_ids: list[int], ranges: list[tuple[date, date]],
                     metric_ids: set[int] | None = None) -> dict[tuple[int, int, date], float | Exception]:
    """Факт каждого источника 1С за отрезки дней (a, b), минуя сохранённые помесячные данные.
    → {(company_id, metric_id, a): значение или ошибка}. Кеш — 5 минут."""
    import time

    q = select(OneCSource).where(OneCSource.is_active.is_(True), OneCSource.company_id.in_(company_ids))
    if metric_ids is not None:
        q = q.where(OneCSource.metric_id.in_(metric_ids))
    sources = list(await session.scalars(q))
    if not sources or not ranges:
        return {}
    conns = api.load_connections()
    out: dict[tuple[int, int, date], float | Exception] = {}
    clients: dict[str, api.Client] = {}
    try:
        for s in sources:
            src = api.Source(s.entity, s.field, s.aggregation, s.filter, s.period_mode, s.date_field, s.multiplier)
            for a, b in ranges:
                key = (s.connection, s.entity, s.field, s.aggregation, s.filter, s.period_mode, s.date_field,
                       s.multiplier, a, b)
                hit = _live_cache.get(key)
                ttl = 60 if isinstance(hit and hit[1], Exception) else LIVE_TTL  # ошибку помним минуту
                if hit and time.monotonic() - hit[0] < ttl:
                    out[(s.company_id, s.metric_id, a)] = hit[1]
                    continue
                conn = conns.get(s.connection)
                if conn is None:
                    out[(s.company_id, s.metric_id, a)] = api.OneCError(f"подключение «{s.connection}» не настроено")
                    continue
                cl = clients.get(conn.name) or clients.setdefault(conn.name, api.Client(conn))
                try:
                    val = (await api.compute(cl, src, a, b)).value
                except api.OneCError as e:
                    out[(s.company_id, s.metric_id, a)] = e
                    _live_cache[key] = (time.monotonic(), e)
                    continue
                _live_cache[key] = (time.monotonic(), val)
                out[(s.company_id, s.metric_id, a)] = val
    finally:
        for cl in clients.values():
            await cl.http.aclose()
    return out


# ---------- загрузка фактов ----------


def _default_period() -> Period:
    cur = month_start(today_local())
    return Period(add_months(cur, -1), cur)  # прошлый месяц тоже: в 1С его могут ещё закрывать


async def _active_sources(session: AsyncSession, metrics: list[str] | None, company: str | None):
    q = (select(OneCSource, MetricDefinition, Company, Department)
         .join(MetricDefinition, MetricDefinition.id == OneCSource.metric_id)
         .join(Company, Company.id == OneCSource.company_id)
         .join(Department, Department.id == MetricDefinition.department_id)
         .where(OneCSource.is_active.is_(True))
         .order_by(Company.sort_order, Department.sort_order, MetricDefinition.sort_order))
    if metrics:
        q = q.where(MetricDefinition.code.in_(metrics))
    if company and company != refs.HOLDING:
        q = q.where(Company.code == company)
    return list(await session.execute(q))


@write_op("onec.load_facts", external=True)
async def load_facts(session: AsyncSession, actor: Actor, *, period: str | None = None,
                     metrics: list[str] | None = None, company: str | None = None,
                     dry_run: bool = False) -> ChangePreview:
    """Подтянуть факты из 1С по настроенным источникам. По умолчанию — прошлый и текущий месяц."""
    _own(actor)
    p = Period.parse(period, today_local()) if period else _default_period()
    if company and company != refs.HOLDING:
        await refs.get_company(session, company)
    for code in metrics or []:
        await _base_metric(session, code)
    rows = await _active_sources(session, metrics, company)
    if not rows:
        raise ValidationError("Нет настроенных источников в 1С для загрузки — сначала настройте их "
                              "(onec_save_source)")
    months = p.months()
    if len(rows) * len(months) > MAX_CALLS:
        raise ValidationError(f"Слишком большая загрузка ({len(rows)} источников × {len(months)} мес.) — "
                              "разбейте на части по периодам")
    by_metric: dict[str, list[str]] = {}
    for _s, m, c, _d in rows:
        by_metric.setdefault(m.name, []).append(c.name)
    preview = ChangePreview(
        title=f"Загрузка фактов из 1С · {p.label}",
        changes=[FieldChange("Показатели", None, "; ".join(f"{k} ({', '.join(v)})" for k, v in by_metric.items())),
                 FieldChange("Куда", None, "факт на сайте и в ботах; планы не меняются")])
    if dry_run:
        return preview

    async with _sync_lock:
        PROGRESS.__init__(running=True, total=len(rows) * len(months), period=p.label, started_at=utcnow(),
                          run_id=PROGRESS.run_id + 1)
        try:
            res = await _load(session, actor, rows, months, p, preview)
        except Exception:
            PROGRESS.result = "Загрузка прервалась с ошибкой — подробности в логе"
            PROGRESS.errors += 1
            raise
        finally:
            PROGRESS.running, PROGRESS.current, PROGRESS.finished_at = False, None, utcnow()
            PROGRESS.done = PROGRESS.total
        return res


async def _load(session, actor, rows, months: list[date], p: Period, preview: ChangePreview) -> ChangePreview:
    conns = api.load_connections()
    values: dict[tuple[int, int, date], float] = {}
    errors: dict[int, str] = {}               # source.id → текст
    clients: dict[str, api.Client] = {}
    try:
        for s, m, c, _d in rows:
            conn = conns.get(s.connection)
            if conn is None:
                errors[s.id] = f"подключение «{s.connection}» больше не настроено в .env"
                continue
            cl = clients.get(conn.name) or clients.setdefault(conn.name, api.Client(conn))
            src = api.Source(s.entity, s.field, s.aggregation, s.filter, s.period_mode, s.date_field, s.multiplier)
            for i, mo in enumerate(months):
                PROGRESS.current = f"{m.name} · {c.name} · {month_label(mo)}"
                try:
                    values[(c.id, m.id, mo)] = (await api.compute(cl, src, mo)).value
                except api.OneCError as e:
                    errors[s.id] = str(e)
                    PROGRESS.errors += 1
                    PROGRESS.done += len(months) - i  # остальные месяцы этого источника пропускаем
                    break
                PROGRESS.done += 1
    finally:
        for cl in clients.values():
            await cl.http.aclose()

    now = utcnow()
    meta = {(c.id, m.id): (m, c) for _s, m, c, _d in rows}
    before, after, changed_lines = {}, {}, []
    existing = {(v.company_id, v.metric_id, v.period): v for v in await session.scalars(
        select(MetricValue).where(MetricValue.period.in_(months),
                                  MetricValue.metric_id.in_({m.id for _s, m, _c, _d in rows})))}
    for (cid, mid, mo), val in values.items():
        row = existing.get((cid, mid, mo))
        val = round(val, 4)
        if row is not None and row.fact is not None and abs(row.fact - val) < 0.005 and row.source == SOURCE:
            continue
        m, c = meta[(cid, mid)]
        key = f"{c.code}/{m.code}/{mo:%Y-%m}"
        before[key] = row.fact if row else None
        after[key] = val
        changed_lines.append(f"• {m.name} · {c.name} · {month_label(mo)}: "
                             f"{fmt_value(row.fact if row else None, m.unit, m.decimals)} → "
                             f"{fmt_value(val, m.unit, m.decimals)}")
        if row is None:
            session.add(MetricValue(company_id=cid, metric_id=mid, period=mo, fact=val, source=SOURCE))
        else:
            row.fact, row.source = val, SOURCE

    # статус синхронизации по компании и отделу
    groups: dict[tuple[int, int], list[OneCSource]] = {}
    for s, _m, c, d in rows:
        s.last_run_at, s.last_error = now, errors.get(s.id)
        groups.setdefault((c.id, d.id), []).append(s)
    for (cid, did), srcs in groups.items():
        errs = [errors[s.id] for s in srcs if s.id in errors]
        st = await session.scalar(select(SyncStatus).where(SyncStatus.source == SOURCE, SyncStatus.company_id == cid,
                                                           SyncStatus.department_id == did))
        if st is None:
            st = SyncStatus(source=SOURCE, company_id=cid, department_id=did)
            session.add(st)
        st.last_attempt_at = now
        if errs:
            st.status, st.message = SyncState.ERROR, errs[0][:500]
        else:
            st.status, st.message, st.last_success_at = SyncState.OK, None, now
        # демо-метка свежести больше не нужна: данные отдела теперь из 1С
        await session.execute(delete(SyncStatus).where(SyncStatus.source == "demo", SyncStatus.company_id == cid,
                                                       SyncStatus.department_id == did))

    ok_sources = len(rows) - len(errors)
    title = (f"Загрузка из 1С · {p.label}: источников {ok_sources} из {len(rows)}, "
             f"изменилось значений: {len(changed_lines)}")
    lines = [title] + changed_lines[:40]
    if len(changed_lines) > 40:
        lines.append(f"• … и ещё {len(changed_lines) - 40}")
    names = {s.id: f"{m.name} · {c.name}" for s, m, c, _d in rows}
    lines += [f"• Ошибка: {names[sid]} — {msg}" for sid, msg in errors.items()]
    if changed_lines or errors:
        await changelog.log(session, actor, entity="metric_value", entity_id=None, action="load_1c",
                            summary="\n".join(lines), before=before or None,
                            after=(after | {"errors": {names[k]: v for k, v in errors.items()}}) or None)
    await session.commit()
    preview.title = title + (f"; ошибок: {len(errors)}" if errors else "")
    PROGRESS.result, PROGRESS.changed = preview.title, len(changed_lines)
    preview.changes = [FieldChange("Изменения", None, x[2:]) for x in changed_lines[:15]] + \
                      [FieldChange("Ошибка", None, f"{names[sid]} — {msg}") for sid, msg in errors.items()]
    return preview


async def has_sources(session: AsyncSession) -> bool:
    return await session.scalar(select(OneCSource.id).where(OneCSource.is_active.is_(True)).limit(1)) is not None


NEW_SOURCE_HISTORY = "half"  # новый источник сразу подтягивает полгода истории


async def has_new_sources(session: AsyncSession) -> bool:
    return await session.scalar(select(OneCSource.id).where(OneCSource.is_active.is_(True),
                                                            OneCSource.last_run_at.is_(None)).limit(1)) is not None


async def scheduled_load(session: AsyncSession, actor: Actor, due: bool = True) -> str | None:
    """Фоновая загрузка. Новые (ещё не загружавшиеся) источники — сразу и с историей;
    остальное — когда подошло время (due). None — загружать нечего."""
    if await has_new_sources(session):
        return (await load_facts(session, actor, period=NEW_SOURCE_HISTORY)).title
    if not due or not await has_sources(session):
        return None
    return (await load_facts(session, actor)).title

