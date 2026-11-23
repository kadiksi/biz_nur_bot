"""Страницы сайта, которые собирает главный бот.

Страница — список блоков (заголовок, текст, KPI, таблица, график, канбан, сводка).
В блоках хранится только «что показать»; цифры берутся из базы при каждом открытии,
поэтому изменение плана через бота сразу видно на сайте. Менять страницы может только владелец.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MetricDefinition, SitePage, SitePageVersion
from app.services import changelog, refs
from app.services.access import can_read_department_somewhere, require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import NotFound, ValidationError

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
WIDTHS = {"full": "во всю ширину", "half": "половина", "third": "треть"}
CHART_KINDS = {"bar", "line"}
MAX_BLOCKS = 30

BLOCK_LABELS = {
    "heading": "Заголовок",
    "text": "Текст",
    "kpis": "Карточки KPI",
    "table": "Таблица план/факт",
    "chart": "График",
    "kanban": "Канбан",
    "summary": "Сводка по отделам",
}

# Ключи, допустимые в каждом типе блока (кроме общих)
_COMMON = {"type", "width", "title", "company"}
_ALLOWED = {
    "heading": {"text"},
    "text": {"text"},
    "kpis": {"metrics"},
    "table": {"department", "metrics"},
    "chart": {"metric", "kind", "compare_companies"},
    "kanban": {"department"},
    "summary": {"departments"},
}


# ---------- валидация ----------


async def _catalog(session: AsyncSession) -> tuple[dict[str, MetricDefinition], dict[str, str]]:
    """(показатели по коду, названия отделов по коду)."""
    metrics = {m.code: m for m in await session.scalars(select(MetricDefinition))}
    depts = {d.code: d.name for d in await refs.list_departments(session)}
    return metrics, depts


def _metric_list(v: Any, metrics: dict[str, MetricDefinition], where: str) -> list[str]:
    if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
        raise ValidationError(f"{where}: metrics — непустой список кодов показателей")
    if len(v) > 12:
        raise ValidationError(f"{where}: не больше 12 показателей в блоке")
    for code in v:
        m = metrics.get(code)
        if m is None:
            raise ValidationError(f"{where}: неизвестный показатель «{code}»")
        if not m.is_visible:
            raise ValidationError(f"{where}: «{code}» — служебный показатель, его не показывают")
    return list(dict.fromkeys(v))


def _text(v: Any, where: str, limit: int) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValidationError(f"{where}: нужен непустой текст")
    if len(v) > limit:
        raise ValidationError(f"{where}: текст длиннее {limit} символов")
    return v.strip()


async def validate_blocks(session: AsyncSession, blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(blocks, list) or not blocks:
        raise ValidationError("На странице должен быть хотя бы один блок")
    if len(blocks) > MAX_BLOCKS:
        raise ValidationError(f"Не больше {MAX_BLOCKS} блоков на странице")
    metrics, depts = await _catalog(session)
    companies = {c.code for c in await refs.list_companies(session)} | {refs.HOLDING}
    out = []
    for i, b in enumerate(blocks, 1):
        where = f"Блок {i}"
        if not isinstance(b, dict) or b.get("type") not in _ALLOWED:
            raise ValidationError(f"{where}: type должен быть одним из: {', '.join(_ALLOWED)}")
        t = b["type"]
        extra = set(b) - _COMMON - _ALLOWED[t]
        if extra:
            raise ValidationError(f"{where} ({t}): лишние поля {', '.join(sorted(extra))}")
        clean: dict[str, Any] = {"type": t, "width": b.get("width") or "full"}
        if clean["width"] not in WIDTHS:
            raise ValidationError(f"{where}: width — full, half или third")
        if b.get("title") is not None:
            clean["title"] = _text(b["title"], f"{where}: title", 100)
        if b.get("company") is not None:
            if b["company"] not in companies:
                raise ValidationError(f"{where}: company — holding или код компании ({', '.join(sorted(companies - {refs.HOLDING})) or 'компаний пока нет'})")
            clean["company"] = b["company"]

        if t in ("heading", "text"):
            clean["text"] = _text(b.get("text"), where, 200 if t == "heading" else 3000)
        elif t == "kpis":
            clean["metrics"] = _metric_list(b.get("metrics"), metrics, where)
        elif t == "table":
            if bool(b.get("department")) == bool(b.get("metrics")):
                raise ValidationError(f"{where}: для таблицы укажите либо department, либо metrics")
            if b.get("department"):
                if b["department"] not in depts:
                    raise ValidationError(f"{where}: неизвестный отдел «{b['department']}»")
                clean["department"] = b["department"]
            else:
                clean["metrics"] = _metric_list(b["metrics"], metrics, where)
        elif t == "chart":
            clean["metric"] = _metric_list([b.get("metric")] if b.get("metric") else None, metrics, where)[0]
            clean["kind"] = b.get("kind") or "bar"
            if clean["kind"] not in CHART_KINDS:
                raise ValidationError(f"{where}: kind — bar или line")
            if b.get("compare_companies"):
                clean["compare_companies"] = True
        elif t == "kanban":
            if b.get("department") not in depts:
                raise ValidationError(f"{where}: для канбана нужен department ({', '.join(depts)})")
            clean["department"] = b["department"]
        elif t == "summary":
            ds = b.get("departments")
            if ds is not None:
                if not isinstance(ds, list) or not ds or any(d not in depts for d in ds):
                    raise ValidationError(f"{where}: departments — список отделов ({', '.join(depts)})")
                clean["departments"] = list(dict.fromkeys(ds))
        out.append(clean)
    return out


def describe_block(b: dict[str, Any], metric_names: dict[str, str], dept_names: dict[str, str],
                   company_names: dict[str, str] | None = None) -> str:
    """Одна строка для предпросмотра в Telegram."""
    t = b["type"]
    parts = [BLOCK_LABELS[t]]
    if t in ("heading", "text"):
        text = b["text"].replace("\n", " ")
        parts.append(f"«{text[:60]}{'…' if len(text) > 60 else ''}»")
    if b.get("title"):
        parts.append(f"«{b['title']}»")
    if b.get("department"):
        parts.append(dept_names.get(b["department"], b["department"]))
    if b.get("metrics"):
        parts.append(", ".join(metric_names.get(m, m) for m in b["metrics"]))
    if b.get("metric"):
        parts.append(metric_names.get(b["metric"], b["metric"])
                     + (", по компаниям" if b.get("compare_companies") else ", план/факт по месяцам")
                     + (", линия" if b.get("kind") == "line" else ""))
    if t == "summary":
        ds = b.get("departments")
        parts.append(", ".join(dept_names[d] for d in ds) if ds else "все отделы")
    if b.get("company"):
        names = {refs.HOLDING: "холдинг", **(company_names or {})}
        parts.append(f"всегда {names.get(b['company'], b['company'])}")
    if b.get("width", "full") != "full":
        parts.append(WIDTHS[b["width"]])
    return " · ".join(parts)


async def describe_blocks(session: AsyncSession, blocks: list[dict[str, Any]]) -> str:
    metrics, depts = await _catalog(session)
    names = {c: m.name for c, m in metrics.items()}
    comps = {c.code: c.name for c in await refs.list_companies(session)}
    return "\n".join(f"  {i}. {describe_block(b, names, depts, comps)}" for i, b in enumerate(blocks, 1))


def _slug(v: Any) -> str:
    if not isinstance(v, str) or not SLUG_RE.match(v):
        raise ValidationError("Адрес страницы (slug) — латиница в нижнем регистре, цифры и дефис, до 40 символов: sales, marketing-2")
    return v


async def _department(session: AsyncSession, v: Any) -> str | None:
    if v is None:
        return None
    return (await refs.get_department(session, v)).code


# ---------- чтение ----------


def can_view(actor: Actor, page: SitePage) -> bool:
    return page.department is None or can_read_department_somewhere(actor, page.department)


async def list_pages(session: AsyncSession, actor: Actor, menu_only: bool = False) -> list[SitePage]:
    q = select(SitePage).order_by(SitePage.sort_order, SitePage.id)
    if menu_only:
        q = q.where(SitePage.in_menu.is_(True))
    return [p for p in await session.scalars(q) if can_view(actor, p)]


async def _find(session: AsyncSession, slug: str) -> SitePage | None:
    return await session.scalar(select(SitePage).where(SitePage.slug == slug))


async def get_page(session: AsyncSession, actor: Actor, slug: str) -> SitePage:
    page = await _find(session, slug)
    if page is None or not can_view(actor, page):
        raise NotFound(f"Страница «{slug}» не найдена")
    return page


async def list_versions(session: AsyncSession, actor: Actor, slug: str) -> list[SitePageVersion]:
    require_owner(actor, "смотреть историю страниц")
    return list(await session.scalars(
        select(SitePageVersion).where(SitePageVersion.slug == slug).order_by(SitePageVersion.version.desc())))


# ---------- запись ----------


PAGE_FIELDS = ["slug", "title", "department", "sort_order", "in_menu", "blocks", "version"]


async def _archive(session: AsyncSession, page: SitePage) -> None:
    session.add(SitePageVersion(slug=page.slug, version=page.version, title=page.title,
                                department=page.department, blocks=page.blocks))


async def _dept_label(session: AsyncSession, code: str | None) -> str:
    if code is None:
        return "все, у кого есть вход"
    return f"кто видит отдел «{(await refs.get_department(session, code)).name}»"


@write_op("site.save_page")
async def save_page(
    session: AsyncSession,
    actor: Actor,
    *,
    slug: str,
    title: str,
    blocks: list[dict[str, Any]],
    department: str | None = UNSET,
    sort_order: int = UNSET,
    in_menu: bool = UNSET,
    dry_run: bool = False,
) -> ChangePreview:
    """Создаёт страницу или целиком заменяет её состав (старая версия сохраняется)."""
    require_owner(actor, "менять сайт")
    slug, title = _slug(slug), _text(title, "Название страницы", 100)
    blocks = await validate_blocks(session, blocks)
    page = await _find(session, slug)
    dept = await _department(session, department) if department is not UNSET else (page.department if page else None)

    if page is None:
        order = sort_order if sort_order is not UNSET else (
            (await session.scalar(select(SitePage.sort_order).order_by(SitePage.sort_order.desc()).limit(1)) or 0) + 10)
        changes = [
            FieldChange("Адрес", None, f"/p/{slug}"),
            FieldChange("Видят", None, await _dept_label(session, dept)),
            FieldChange("В меню", None, "да" if in_menu is UNSET or in_menu else "нет"),
            FieldChange("Блоки", None, f"{len(blocks)}\n{await describe_blocks(session, blocks)}"),
        ]
        preview = ChangePreview(title=f"Новая страница сайта «{title}»", changes=changes)
        if dry_run:
            return preview
        # после удаления адрес может вернуться: продолжаем нумерацию версий, а не начинаем с 1
        last = await session.scalar(select(func.max(SitePageVersion.version)).where(SitePageVersion.slug == slug))
        page = SitePage(slug=slug, title=title, department=dept, sort_order=order,
                        in_menu=True if in_menu is UNSET else bool(in_menu), blocks=blocks, version=(last or 0) + 1)
        session.add(page)
        await session.flush()
        await changelog.log(session, actor, entity="site_page", entity_id=page.id, action="create",
                            summary=preview.render(), after=changelog.snapshot(page, PAGE_FIELDS))
        await session.commit()
        preview.entity_id = page.id
        return preview

    changes = []
    if title != page.title:
        changes.append(FieldChange("Название", page.title, title))
    if dept != page.department:
        changes.append(FieldChange("Видят", await _dept_label(session, page.department), await _dept_label(session, dept)))
    if sort_order is not UNSET and sort_order != page.sort_order:
        changes.append(FieldChange("Порядок в меню", str(page.sort_order), str(sort_order)))
    if in_menu is not UNSET and bool(in_menu) != page.in_menu:
        changes.append(FieldChange("В меню", "да" if page.in_menu else "нет", "да" if in_menu else "нет"))
    if blocks != page.blocks:
        changes.append(FieldChange("Блоки", str(len(page.blocks)),
                                   f"{len(blocks)}\n{await describe_blocks(session, blocks)}"))
    if not changes:
        raise ValidationError("Страница уже выглядит именно так — менять нечего")
    preview = ChangePreview(title=f"Изменение страницы «{page.title}» (/p/{slug})", changes=changes)
    if dry_run:
        return preview

    before = changelog.snapshot(page, PAGE_FIELDS)
    await _archive(session, page)
    page.title, page.department, page.blocks, page.version = title, dept, blocks, page.version + 1
    if sort_order is not UNSET:
        page.sort_order = sort_order
    if in_menu is not UNSET:
        page.in_menu = bool(in_menu)
    await changelog.log(session, actor, entity="site_page", entity_id=page.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(page, PAGE_FIELDS))
    await session.commit()
    preview.entity_id = page.id
    return preview


@write_op("site.update_page")
async def update_page(
    session: AsyncSession,
    actor: Actor,
    *,
    slug: str,
    title: str = UNSET,
    department: str | None = UNSET,
    sort_order: int = UNSET,
    in_menu: bool = UNSET,
    dry_run: bool = False,
) -> ChangePreview:
    """Название, видимость и место в меню — без замены блоков."""
    require_owner(actor, "менять сайт")
    page = await _find(session, slug)
    if page is None:
        raise NotFound(f"Страница «{slug}» не найдена")
    new: dict[str, Any] = {}
    changes = []
    if title is not UNSET and _text(title, "Название страницы", 100) != page.title:
        new["title"] = title.strip()
        changes.append(FieldChange("Название", page.title, new["title"]))
    if department is not UNSET and (d := await _department(session, department)) != page.department:
        new["department"] = d
        changes.append(FieldChange("Видят", await _dept_label(session, page.department), await _dept_label(session, d)))
    if sort_order is not UNSET and sort_order != page.sort_order:
        new["sort_order"] = int(sort_order)
        changes.append(FieldChange("Порядок в меню", str(page.sort_order), str(sort_order)))
    if in_menu is not UNSET and bool(in_menu) != page.in_menu:
        new["in_menu"] = bool(in_menu)
        changes.append(FieldChange("В меню", "да" if page.in_menu else "нет", "да" if in_menu else "нет"))
    if not changes:
        raise ValidationError("Нечего менять: новые значения совпадают с текущими")
    preview = ChangePreview(title=f"Изменение страницы «{page.title}» (/p/{slug})", changes=changes)
    if dry_run:
        return preview
    before = changelog.snapshot(page, list(new))
    for k, v in new.items():
        setattr(page, k, v)
    await changelog.log(session, actor, entity="site_page", entity_id=page.id, action="update",
                        summary=preview.render(), before=before, after=changelog.snapshot(page, list(new)))
    await session.commit()
    preview.entity_id = page.id
    return preview


@write_op("site.delete_page")
async def delete_page(session: AsyncSession, actor: Actor, *, slug: str, dry_run: bool = False) -> ChangePreview:
    require_owner(actor, "менять сайт")
    page = await _find(session, slug)
    if page is None:
        raise NotFound(f"Страница «{slug}» не найдена")
    preview = ChangePreview(title="Удаление страницы сайта",
                            changes=[FieldChange("Страница", f"«{page.title}» (/p/{slug})", None)])
    if dry_run:
        return preview
    before = changelog.snapshot(page, PAGE_FIELDS)
    await _archive(session, page)  # чтобы можно было вернуть
    await session.delete(page)
    await changelog.log(session, actor, entity="site_page", entity_id=page.id, action="delete",
                        summary=preview.render(), before=before)
    await session.commit()
    preview.entity_id = page.id
    return preview


@write_op("site.restore_page")
async def restore_page(session: AsyncSession, actor: Actor, *, slug: str, version: int,
                       dry_run: bool = False) -> ChangePreview:
    """Вернуть прошлую версию (в том числе удалённой страницы)."""
    require_owner(actor, "менять сайт")
    old = await session.scalar(select(SitePageVersion).where(SitePageVersion.slug == slug,
                                                             SitePageVersion.version == version))
    if old is None:
        raise NotFound(f"У страницы «{slug}» нет версии {version}")
    p = await save_page(session, actor, slug=slug, title=old.title, blocks=old.blocks,
                        department=old.department, dry_run=dry_run)
    p.title = f"Возврат страницы «{old.title}» к версии {version}"
    return p
