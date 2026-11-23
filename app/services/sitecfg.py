"""Настройки сайта: название, цвета, период и компания по умолчанию. Код при этом не меняется."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, SiteSetting
from app.services import changelog
from app.services.access import require_owner
from app.services.changes import UNSET, ChangePreview, FieldChange, write_op
from app.services.context import Actor
from app.services.errors import ValidationError
from app.services.periods import PRESETS, Period
from app.timeutil import utcnow

DEFAULTS: dict[str, Any] = {
    "brand_name": "Corner Group",
    "brand_subtitle": "Оперативные данные",
    "brand_color": "#212347",     # боковое меню и акцент в светлой теме
    "accent_dark": "#8f93d6",     # акцент в тёмной теме (должен быть светлым)
    "default_period": "month",
    "default_company": None,      # None — холдинг (или первая доступная компания)
    "custom_css": "",             # свои стили поверх встроенных: любой элемент интерфейса
    "texts": {},                  # замены надписей: ключ из TEXTS → текст
}
CSS_HISTORY = 10
# Надписи интерфейса: ключ → текст по умолчанию. Владелец меняет любые через бота.
TEXTS: dict[str, str] = {
    "menu": "Меню",
    "menu_sections": "Разделы",
    "theme_toggle": "Сменить тему",
    "logout": "Выйти",
    "holding": "Холдинг",
    "period_month": "Текущий месяц",
    "period_prev_month": "Прошлый месяц",
    "period_quarter": "Квартал",
    "period_half": "Полгода",
    "period_ytd": "С начала года",
    "period_custom": "Свои даты…",
    "show": "Показать",
    "sync_label": "Обновление базы из 1С",
    "sync_done": "Данные из 1С обновлены",
    "sync_errors": "Обновление из 1С закончилось с ошибками",
    "hint_partial": "Период с точными датами: неполные месяцы считаются прямо из 1С за эти дни, план — "
                    "пропорционально дням. Показатели, которые не подключены к 1С, за неполный месяц не показываются.",
    "hint_no_companies": "Компании ещё не добавлены — напишите боту, например: «добавь компанию …». Цифры "
                         "появятся, когда к компаниям подключат 1С или зададут планы.",
    "kpi_plan": "план",
    "table_metric": "Показатель",
    "table_plan": "План",
    "table_fact": "Факт",
    "table_dev": "Откл.",
    "table_pct": "%",
    "lower_better": "Меньше — лучше",
    "due": "до",
    "empty_title": "Сайт пока пустой",
    "empty_text": "Страницы собирает главный бот. Напишите ему, например:",
    "login_needed_title": "Нужен вход",
    "login_needed_text": "Отправьте главному боту команду /site — он пришлёт ссылку для входа. Ссылка действует "
                         "10 минут, вход запоминается на 30 дней.",
    "login_title": "Вход",
    "login_button": "Войти",
    "login_expired_title": "Ссылка устарела",
    "login_expired_text": "Ссылка уже использована или прошло больше 10 минут. Отправьте боту /site ещё раз.",
    "not_found_title": "Страница не найдена",
    "not_found_text": "Её удалили или у вас нет к ней доступа.",
    "kanban_move_error": "Не удалось переместить карточку",
    "no_connection": "Нет связи с сервером",
}
JS_TEXTS = ["sync_done", "sync_errors", "kanban_move_error", "no_connection"]

LABELS = {
    "brand_name": "Название", "brand_subtitle": "Подзаголовок", "brand_color": "Цвет меню и акцента",
    "accent_dark": "Акцент в тёмной теме", "default_period": "Период по умолчанию",
    "default_company": "Компания по умолчанию",
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _luma(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


async def get_all(session: AsyncSession) -> dict[str, Any]:
    stored = {s.key: s.value for s in await session.scalars(select(SiteSetting))}
    return {k: stored.get(k, v) for k, v in DEFAULTS.items()}


async def _check(session: AsyncSession, key: str, v: Any) -> Any:
    if v is None:
        return None  # вернуть по умолчанию
    if key in ("brand_name", "brand_subtitle"):
        if not isinstance(v, str) or len(v.strip()) > 40 or (key == "brand_name" and not v.strip()):
            raise ValidationError(f"{LABELS[key]}: текст до 40 символов")
        return v.strip()
    if key in ("brand_color", "accent_dark"):
        if not isinstance(v, str) or not _HEX.match(v.strip()):
            raise ValidationError(f"{LABELS[key]}: цвет в формате #RRGGBB, например #212347")
        v = v.strip().lower()
        if key == "brand_color" and _luma(v) > 0.45:
            raise ValidationError("Цвет меню слишком светлый: на нём не будет видно белый текст. Нужен тёмный цвет")
        if key == "accent_dark" and _luma(v) < 0.35:
            raise ValidationError("Акцент тёмной темы слишком тёмный: на тёмном фоне его не будет видно")
        return v
    if key == "default_period":
        if v not in PRESETS:
            try:
                Period.parse(v)
            except ValidationError:
                raise ValidationError("Период по умолчанию: month, prev_month, quarter, half, ytd "
                                      "или конкретный (ГГГГ-ММ..ГГГГ-ММ)") from None
        return v
    if key == "default_company":
        if v != "holding" and not await session.scalar(select(Company.id).where(Company.code == v)):
            raise ValidationError(f"Компании «{v}» нет")
        return None if v == "holding" else v
    raise ValidationError(f"Неизвестная настройка {key}")


def _show(v: Any) -> str:
    return "по умолчанию" if v is None else str(v)


@write_op("site.update_settings")
async def update_settings(session: AsyncSession, actor: Actor, *, brand_name: str | None = UNSET,
                          brand_subtitle: str | None = UNSET, brand_color: str | None = UNSET,
                          accent_dark: str | None = UNSET, default_period: str | None = UNSET,
                          default_company: str | None = UNSET, dry_run: bool = False) -> ChangePreview:
    """null у поля — вернуть значение по умолчанию."""
    require_owner(actor, "менять настройки сайта")
    given = {"brand_name": brand_name, "brand_subtitle": brand_subtitle, "brand_color": brand_color,
             "accent_dark": accent_dark, "default_period": default_period, "default_company": default_company}
    current = await get_all(session)
    stored = {s.key: s for s in await session.scalars(select(SiteSetting))}
    new: dict[str, Any] = {}
    for k, v in given.items():
        if v is UNSET:
            continue
        v = await _check(session, k, v)
        if (DEFAULTS[k] if v is None else v) != current[k]:
            new[k] = v
    if not new:
        raise ValidationError("Менять нечего")
    preview = ChangePreview(
        title="Настройки сайта",
        changes=[FieldChange(LABELS[k], _show(current[k]), _show(DEFAULTS[k] if v is None else v))
                 for k, v in new.items()])
    if dry_run:
        return preview
    before = {k: current[k] for k in new}
    for k, v in new.items():
        if v is None:
            if k in stored:
                await session.delete(stored[k])
        elif k in stored:
            stored[k].value = v
        else:
            session.add(SiteSetting(key=k, value=v))
    await changelog.log(session, actor, entity="site_settings", entity_id=None, action="update",
                        summary=preview.render(), before=before,
                        after={k: (DEFAULTS[k] if v is None else v) for k, v in new.items()})
    await session.commit()
    return preview


# ---------- свои стили и надписи ----------

_CSS_BANNED = re.compile(r"<|@import|url\s*\(|expression\s*\(|javascript:|behavior\s*:|-moz-binding|@charset|image-set|@font-face|@namespace|src\s*\(|\\",
                         re.IGNORECASE)
MAX_CSS = 30_000


def check_css(css: str) -> str:
    if not isinstance(css, str):
        raise ValidationError("Стили — это текст CSS")
    css = css.strip()
    if len(css) > MAX_CSS:
        raise ValidationError(f"Стили длиннее {MAX_CSS} символов")
    bad = _CSS_BANNED.search(css)
    if bad:
        raise ValidationError(f"В стилях нельзя «{bad.group(0)}»: внешние файлы, картинки по ссылке и скрипты "
                              "запрещены, чтобы сайт не обращался наружу")
    depth = 0
    for ch in css:
        depth += ch == "{"
        depth -= ch == "}"
        if depth < 0:
            break
    if depth != 0:
        raise ValidationError("В стилях не сходятся фигурные скобки { }")
    return css


async def _row(session: AsyncSession, key: str) -> SiteSetting | None:
    return await session.get(SiteSetting, key)


async def _put(session: AsyncSession, key: str, value: Any) -> None:
    row = await _row(session, key)
    if row is None:
        session.add(SiteSetting(key=key, value=value))
    else:
        row.value = value


async def css_history(session: AsyncSession) -> list[dict]:
    row = await _row(session, "custom_css_history")
    return list(row.value) if row else []


def _css_diff(before: str, after: str, limit: int = 12) -> list[FieldChange]:
    import difflib

    lines = [ln for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
             if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and ln[1:].strip()]
    out = [FieldChange("Добавлено" if ln[0] == "+" else "Убрано", None, ln[1:].strip()[:120]) for ln in lines[:limit]]
    if len(lines) > limit:
        out.append(FieldChange("И ещё", None, f"{len(lines) - limit} строк"))
    return out


@write_op("site.set_css")
async def set_css(session: AsyncSession, actor: Actor, *, css: str, dry_run: bool = False) -> ChangePreview:
    """Полностью заменить свои стили сайта. Пустая строка — убрать все свои стили."""
    require_owner(actor, "менять вид сайта")
    css = check_css(css)
    current = (await get_all(session))["custom_css"]
    if css == current:
        raise ValidationError("Стили не изменились")
    preview = ChangePreview(title="Свои стили сайта" + (" — убрать все" if not css else ""),
                            changes=_css_diff(current, css) or [FieldChange("Стили", None, "переформатированы")])
    if dry_run:
        return preview
    if current:
        hist = [{"css": current, "saved": utcnow().isoformat(timespec="minutes")}] + await css_history(session)
        await _put(session, "custom_css_history", hist[:CSS_HISTORY])
    await _put(session, "custom_css", css)
    await changelog.log(session, actor, entity="site_settings", entity_id=None, action="set_css",
                        summary=preview.render(), before={"custom_css": current}, after={"custom_css": css})
    await session.commit()
    return preview


@write_op("site.restore_css")
async def restore_css(session: AsyncSession, actor: Actor, *, version: int, dry_run: bool = False) -> ChangePreview:
    """Вернуть прошлые стили: 1 — предыдущие, 2 — перед ними и т. д."""
    require_owner(actor, "менять вид сайта")
    hist = await css_history(session)
    if not 1 <= int(version) <= len(hist):
        raise ValidationError(f"Прошлых версий стилей: {len(hist)}" if hist else "Прошлых версий стилей нет")
    preview = await set_css(session, actor, css=hist[int(version) - 1]["css"], dry_run=dry_run)
    preview.title = f"Вернуть стили сайта к версии от {hist[int(version) - 1]['saved'].replace('T', ' ')}"
    return preview


@write_op("site.update_texts")
async def update_texts(session: AsyncSession, actor: Actor, *, texts: dict[str, str | None],
                       dry_run: bool = False) -> ChangePreview:
    """Заменить надписи интерфейса. null — вернуть текст по умолчанию."""
    require_owner(actor, "менять надписи сайта")
    if not isinstance(texts, dict) or not texts:
        raise ValidationError("Передайте надписи: {ключ: текст}")
    unknown = [k for k in texts if k not in TEXTS]
    if unknown:
        raise ValidationError(f"Нет таких надписей: {', '.join(unknown)}. Список — в get_ui_map")
    current: dict[str, str] = dict((await get_all(session))["texts"] or {})
    new = dict(current)
    diff = []
    for k, v in texts.items():
        if v is None:
            if k in new:
                diff.append(FieldChange(k, new.pop(k), TEXTS[k]))
            continue
        if not isinstance(v, str) or len(v) > 500:
            raise ValidationError(f"{k}: текст до 500 символов")
        v = v.strip()
        if v != (current.get(k) or TEXTS[k]):
            diff.append(FieldChange(k, current.get(k) or TEXTS[k], v or "(пусто)"))
            new[k] = v
    if not diff:
        raise ValidationError("Менять нечего")
    preview = ChangePreview(title="Надписи сайта", changes=diff)
    if dry_run:
        return preview
    await _put(session, "texts", new)
    await changelog.log(session, actor, entity="site_settings", entity_id=None, action="update_texts",
                        summary=preview.render(), before={"texts": current}, after={"texts": new})
    await session.commit()
    return preview


def texts_for(cfg: dict[str, Any]) -> dict[str, str]:
    over = cfg.get("texts") or {}
    return {k: over.get(k, v) for k, v in TEXTS.items()}


# Карта интерфейса для бота: что как называется и чем это выбрать в CSS.
UI_MAP: list[dict[str, str]] = [
    {"selector": ":root", "what": "переменные темы: --accent, --bg, --surface, --surface-2, --ink, --muted, --line, "
     "--good/--warn/--bad (+ -soft), --plan, --fact, --c1..--c3 (цвета графиков), --radius, --shadow, --serif, --sans. "
     "Тёмная тема: :root[data-theme=dark] и @media (prefers-color-scheme: dark)"},
    {"selector": "body", "what": "вся страница, фон"},
    {"selector": ".side", "what": "боковое меню (на телефоне — выезжающее)"},
    {"selector": ".brand, .brand__name, .brand__sub", "what": "название и подзаголовок в меню"},
    {"selector": ".menu, .menu__item, .menu__item.is-active, .menu__mark", "what": "пункты меню, активный пункт"},
    {"selector": ".side__foot, .side__user, .icon-btn", "what": "низ меню: имя, кнопки темы и выхода"},
    {"selector": ".mtop", "what": "верхняя полоса на телефоне с кнопкой ☰"},
    {"selector": ".main, .wrap", "what": "основная область и её ширина"},
    {"selector": ".page-head, .page-title, .page-meta", "what": "заголовок страницы и строка «Холдинг · период»"},
    {"selector": ".fresh, .fresh.is-stale, .fresh.is-error", "what": "метка свежести данных «обновлено …»"},
    {"selector": ".sync, .sync__label, .sync__pct, .sync__track, .sync__fill, .sync__detail",
     "what": "полоса прогресса обновления базы из 1С (видна только во время обновления)"},
    {"selector": ".filters, .seg, .seg__item, .seg__item.is-active", "what": "переключатель компаний"},
    {"selector": ".select, .dates, .btn-small", "what": "выбор периода и полей «свои даты»"},
    {"selector": ".hint", "what": "пояснения на странице"},
    {"selector": ".grid, .block, .w-full, .w-half, .w-third", "what": "сетка блоков и ширины блоков"},
    {"selector": ".block--kpis, .block--table, .block--chart, .block--kanban, .block--summary, .block--text, "
     ".block--heading", "what": "блок определённого типа"},
    {"selector": ".block__head, .block__error", "what": "заголовок блока, ошибка в блоке"},
    {"selector": ".kpis, .kpi, .kpi--good, .kpi--warn, .kpi--bad, .kpi__name, .kpi__fact, .kpi__plan, .bar",
     "what": "карточки показателей и полоска выполнения"},
    {"selector": ".sum-groups, .sum-group, .sum-group__title", "what": "сводка по отделам"},
    {"selector": ".table-wrap, table.pf, .pf th, .pf td, .num, .pill, .pill--good, .pill--warn, .pill--bad, .hide-sm",
     "what": "таблица план/факт и значки %"},
    {"selector": ".chart", "what": "область графика (высота)"},
    {"selector": ".board, .col, .col__head, .col__count, .col__list, .card, .card__title, .card__desc, .card__meta, "
     ".tag, .tag--high, .tag--overdue, .card__who", "what": "канбан"},
    {"selector": ".section-title, .prose", "what": "блоки «подзаголовок» и «текст»"},
    {"selector": ".notice", "what": "окна входа, пустого сайта, «не найдено»"},
    {"selector": ".toast", "what": "всплывающее сообщение внизу"},
]
