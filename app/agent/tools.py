"""Инструменты Claude поверх app/services.

Чтение выполняется сразу. Запись НЕ выполняется: инструмент делает preview, а
бот показывает его с кнопкой «Подтвердить» и вызывает apply только после нажатия.
Права проверяет слой services — инструменты лишь переводят вход/выход.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, Department, Employee
from app.services import (changelog, changes, connectors, employees, kanban, metrics, onec, recurring, refs, secrets,
                          site, sitecfg, sync)
from app.services.context import Actor
from app.services.errors import ServiceError
from app.services.format import fmt_dt
from app.services.kanban import PRIORITY_LABELS, STATUS_LABELS
from app.services.periods import Period, today_local

# ---------- схемы ----------

COMPANY = {"type": "string", "description": "Код компании из каталога или holding (все компании вместе)"}
COMPANY_EXACT = {"type": "string", "description": "Код одной компании из каталога"}
DEPARTMENT = {"type": "string", "description": "Код отдела из каталога (sales — ОП и т. д.)"}
PERIOD = {"type": "string", "description": (
    "Период: month (текущий месяц), prev_month, quarter (текущий квартал), half (последние 6 мес.), "
    "ytd (с начала года), ГГГГ-ММ (один месяц), ГГГГ-ММ..ГГГГ-ММ (диапазон месяцев) или "
    "ГГГГ-ММ-ДД..ГГГГ-ММ-ДД (точные даты: неполные месяцы считаются прямо из 1С, план — пропорционально дням)")}
CARD_STATUS = {"type": "string", "enum": ["backlog", "in_progress", "review", "done"],
               "description": "backlog — Бэклог, in_progress — В работе, review — На проверке, done — Готово"}
PRIORITY = {"type": "string", "enum": ["low", "normal", "high"]}
CRON = {"type": "string", "description": "Расписание cron из 5 полей: минута час день месяц день_недели "
        "(1 — понедельник, 0/7 — воскресенье). Время — местное. Пример: «0 10 * * 1» — по понедельникам в 10:00"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SLUG = {"type": "string", "description": "Адрес страницы: латиница в нижнем регистре, цифры, дефис (sales, marketing-2)"}
BLOCK = {
    "type": "object",
    "description": (
        "Блок страницы. Поля по типу: heading/text — text; kpis — metrics (1–12 кодов); "
        "table — department ИЛИ metrics; chart — metric, kind (bar|line), compare_companies "
        "(на холдинге — факт каждой компании вместо план/факт); kanban — department; "
        "summary — departments (необязательно, по умолчанию все). "
        "Общие необязательные: width (full|half|third), title, company (закрепить компанию, "
        "иначе — выбранная на сайте)."),
    "properties": {
        "type": {"type": "string", "enum": ["heading", "text", "kpis", "table", "chart", "kanban", "summary"]},
        "width": {"type": "string", "enum": ["full", "half", "third"]},
        "title": {"type": "string"},
        "company": {"type": "string", "description": "holding или код компании"},
        "text": {"type": "string"},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "metric": {"type": "string"},
        "kind": {"type": "string", "enum": ["bar", "line"]},
        "compare_companies": {"type": "boolean"},
        "department": DEPARTMENT,
        "departments": {"type": "array", "items": DEPARTMENT},
    },
    "required": ["type"],
    "additionalProperties": False,
}
PAGE_DEPT = {"type": ["string", "null"],
             "description": "Код отдела страницы: её видят только те, кто видит этот отдел. null — общая страница для всех"}


CONN = {"type": "string", "description": "Имя подключения к 1С из onec_connections (ut, buh…)"}
ENTITY = {"type": "string", "description": (
    "Объект 1С в OData: Catalog_Номенклатура, Document_РеализацияТоваровУслуг, "
    "AccumulationRegister_Продажи/Turnovers, AccumulationRegister_ДенежныеСредства/Balance, "
    "InformationRegister_ЦеныНоменклатуры/SliceLast")}
SOURCE_PROPS = {
    "entity": ENTITY,
    "field": {"type": "string", "description": "Числовое поле для суммы: ресурс (СуммаTurnover, СуммаBalance) "
              "или реквизит документа (СуммаДокумента). Для count не нужно"},
    "aggregation": {"type": "string", "enum": ["sum", "count"], "description": "sum — сумма поля, count — число строк"},
    "filter": {"type": "string", "description": (
        "OData $filter: Организация_Key eq guid'…' and Posted eq true and DeletionMark eq false. "
        "Можно {start}/{end}/{next} — начало и конец месяца, начало следующего")},
    "period_mode": {"type": "string", "enum": ["turnovers", "balance", "date_field", "none"], "description": (
        "turnovers — обороты за месяц (…/Turnovers, параметры периода подставятся сами); "
        "balance — остаток на конец месяца (…/Balance или …/SliceLast); "
        "date_field — строки, у которых date_field внутри месяца (документы); none — без периода")},
    "date_field": {"type": "string", "description": "Поле даты для period_mode=date_field, обычно Date"},
    "multiplier": {"type": "number", "description": "Множитель результата: -1 — сменить знак, 0.001 — в тысячи. По умолчанию 1"},
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    op: str | None = None        # для записи — имя операции в app.services.changes
    owner_only: bool = False

    @property
    def is_write(self) -> bool:
        return self.op is not None

    def to_api(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


READ_TOOLS = [
    ToolSpec("get_summary", "Ключевые показатели всех отделов (план, факт, % выполнения) за период — для раздела «Сводка».",
             _obj({"company": COMPANY, "period": PERIOD}, ["company", "period"])),
    ToolSpec("get_department_metrics", "Все показатели одного отдела: план, факт, отклонение, % выполнения и помесячная динамика.",
             _obj({"company": COMPANY, "department": DEPARTMENT, "period": PERIOD}, ["company", "department", "period"])),
    ToolSpec("get_metric", "Один показатель по коду с помесячной динамикой. Для сравнения компаний вызывай по разу на компанию.",
             _obj({"company": COMPANY, "metric": {"type": "string", "description": "Код показателя из каталога"},
                   "period": PERIOD}, ["company", "metric", "period"])),
    ToolSpec("get_freshness", "Когда данные последний раз обновлялись из 1С и были ли ошибки синхронизации.",
             _obj({"company": COMPANY, "department": DEPARTMENT}, ["company"])),
    ToolSpec("get_kanban", "Карточки канбана отдела по колонкам (с id, исполнителем, сроком, приоритетом).",
             _obj({"company": COMPANY, "department": DEPARTMENT}, ["company", "department"])),
    ToolSpec("list_employees", "Сотрудники: id, Telegram ID, роль, отдел, компания, активность, дополнительные доступы.",
             _obj({"department": DEPARTMENT, "include_inactive": {"type": "boolean"}}, [])),
    ToolSpec("list_recurring_tasks", "Регулярные задачи с расписанием и следующим запуском.",
             _obj({"department": DEPARTMENT}, [])),
    ToolSpec("get_change_log", "Журнал последних изменений: кто, когда, через какой канал, что поменял.",
             _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 100},
                   "entity": {"type": "string", "enum": ["metric_value", "kanban_card", "recurring_task",
                                                         "employee", "employee_access", "sync_status",
                                                         "site_page", "onec_source", "company",
                                                         "site_settings", "department", "metric_definition",
                                                         "service_connection", "service_request", "google_sheet", "secret"]}}, []),
             owner_only=True),
    ToolSpec("list_pages", "Страницы сайта: адрес, название, отдел, место в меню, версия, состав блоков кратко.",
             _obj({}, []), owner_only=True),
    ToolSpec("get_page", "Полный состав страницы сайта (блоки как JSON) и список прошлых версий. "
             "Вызывай перед изменением существующей страницы.",
             _obj({"slug": SLUG}, ["slug"]), owner_only=True),
    ToolSpec("get_site_settings", "Настройки сайта: название, подзаголовок, цвета, период и компания по умолчанию.",
             _obj({}, []), owner_only=True),
    ToolSpec("get_ui_map", "Карта интерфейса сайта: CSS-селекторы всех элементов с описанием, переменные темы, "
             "все надписи (ключ, текст по умолчанию, текущий), текущие свои стили и их прошлые версии. "
             "Вызывай перед любым изменением вида сайта.", _obj({}, []), owner_only=True),
    ToolSpec("list_services", "Внешние сервисы: сохранённые подключения (адрес, способ входа, есть ли ключ в .env, "
             "адрес сервисного аккаунта Google), имена ключей в .env (без значений) и ожидающие запросы ключей.",
             _obj({}, []), owner_only=True),
    ToolSpec("request_secret", "Запросить у владельца ключ, токен, пароль или файл доступа. Создаёт ссылку на "
             "страницу сайта, где владелец вводит значение; оно пишется в .env под именем key, тебе не показывается. "
             "Когда владелец сохранит, ты получишь сообщение [бот] и продолжишь задачу сам. Ссылку отправь владельцу "
             "вместе с понятной пошаговой инструкцией, где взять значение.",
             _obj({"key": {"type": "string", "description": "Имя в .env: AMO_TOKEN, GSHEETS_KEY"},
                   "title": {"type": "string", "description": "Что запрашиваем, коротко: «Токен amoCRM»"},
                   "instructions": {"type": "string", "description": "Где взять — по шагам, по строке на шаг"},
                   "kind": {"type": "string", "enum": ["text", "json"],
                            "description": "text — строка (токен, пароль); json — файл ключа (Google)"}},
                  ["key", "title"]), owner_only=True),
    ToolSpec("read_file", "Прочитать строки присланного файла (Excel, CSV, текст) по file_id из сообщения "
             "[файл …]. В самом сообщении — только начало каждого листа; остальное читай этим инструментом.",
             _obj({"file_id": {"type": "string"}, "sheet": {"type": "string", "description": "Имя листа"},
                   "start_row": {"type": "integer", "minimum": 1,
                                 "description": "С какой строки (с 1). Для текста и расшифровок — с какой тысячи символов"},
                   "rows": {"type": "integer", "minimum": 1, "maximum": 400}}, ["file_id"]), owner_only=True),
    ToolSpec("onec_request_connection", "Подключить новую базу 1С. Создаёт ссылку на форму сайта: владелец вводит "
             "адрес OData, логин и пароль, сайт проверяет связь и сам пишет подключение в .env. Пароль ты не видишь. "
             "Когда сохранит — придёт [бот], и подключение будет в onec_connections. Уже существующие не меняются.",
             _obj({"name": {"type": "string", "description": "Короткое имя латиницей: velse, ut2"},
                   "url": {"type": "string", "description": "Адрес OData, если известен — подставится в форму"},
                   "user": {"type": "string", "description": "Логин, если известен — подставится в форму"},
                   "instructions": {"type": "string", "description": "Что ввести и где взять, по строке на шаг"}},
                  ["name"]), owner_only=True),
    ToolSpec("service_request", "GET-запрос к сохранённому подключению: прочитать данные внешнего сервиса. "
             "Ключ подставится сам. Для изменений — service_write.",
             _obj({"connection": {"type": "string"}, "path": {"type": "string", "description": "Путь после базового адреса: /api/v4/leads"},
                   "query": {"type": "object", "description": "Параметры запроса"},
                   "method": {"type": "string", "enum": ["GET", "HEAD"]}}, ["connection"]), owner_only=True),
    ToolSpec("sheets_read", "Прочитать Google Таблицу (ссылка или id). С connection (auth=google) — любую, к которой "
             "открыт доступ сервисному аккаунту; без connection — только открытую «всем, у кого есть ссылка».",
             _obj({"spreadsheet": {"type": "string"},
                   "range": {"type": "string", "description": "Лист или диапазон: 'Продажи' или 'Продажи'!A1:F200"},
                   "connection": {"type": "string"}}, ["spreadsheet"]), owner_only=True),
    ToolSpec("onec_connections", "Подключения к базам 1С из .env и проверка связи с каждой (без паролей).",
             _obj({}, []), owner_only=True),
    ToolSpec("onec_find_objects", "Поиск объектов, опубликованных в OData базы 1С: справочники, документы, регистры. "
             "Ищет по словам в имени (кириллица): «продаж», «выручк», «денежн».",
             _obj({"connection": CONN, "search": {"type": "string"},
                   "kind": {"type": "string", "enum": ["Catalog", "Document", "AccumulationRegister",
                                                       "InformationRegister", "AccountingRegister",
                                                       "ChartOfAccounts", "ChartOfCharacteristicTypes", "Constant"]}},
                  ["connection"]), owner_only=True),
    ToolSpec("onec_describe", "Поля объекта 1С с типами, а для регистров — доступные виртуальные таблицы "
             "(Turnovers, Balance, SliceLast).",
             _obj({"connection": CONN, "entity": ENTITY}, ["connection", "entity"]), owner_only=True),
    ToolSpec("onec_query", "Прочитать до 50 строк объекта 1С — посмотреть, какие там данные. "
             "Для виртуальных таблиц укажи параметры сам: …/Turnovers(StartPeriod=datetime'2026-11-01T00:00:00',"
             "EndPeriod=datetime'2026-11-30T23:59:59').",
             _obj({"connection": CONN, "entity": ENTITY, "select": {"type": "string", "description": "Поля через запятую"},
                   "filter": {"type": "string", "description": "OData $filter"},
                   "orderby": {"type": "string"}, "expand": {"type": "string"},
                   "top": {"type": "integer", "minimum": 1, "maximum": 50}}, ["connection", "entity"]),
             owner_only=True),
    ToolSpec("onec_test_source", "Посчитать значение показателя за месяц по описанию источника в 1С, ничего не "
             "сохраняя. Вызывай перед onec_save_source и сверяй с тем, что ожидает владелец.",
             _obj({"connection": CONN, **SOURCE_PROPS,
                   "month": {"type": "string", "description": "ГГГГ-ММ, по умолчанию текущий"}},
                  ["connection", "entity", "period_mode"]), owner_only=True),
    ToolSpec("onec_list_sources", "Настроенные источники показателей в 1С: откуда берётся каждый факт, "
             "когда загружался и была ли ошибка.", _obj({}, []), owner_only=True),
]

WRITE_TOOLS = [
    ToolSpec("set_plan", "Подготовить изменение плана показателя на месяц. Только базовые показатели, не производные.",
             _obj({"company": COMPANY_EXACT,
                   "metric": {"type": "string", "description": "Код базового показателя"},
                   "month": {"type": "string", "description": "ГГГГ-ММ"},
                   "value": {"type": "number", "description": "Новый план в единицах показателя (₽ — в рублях, не в млн)"}},
                  ["company", "metric", "month", "value"]),
             op="plans.set_plan", owner_only=True),
    ToolSpec("set_facts", "Подготовить внесение фактов показателя по месяцам из файла владельца (Excel, CSV) — "
             "для показателей, которых нет в 1С (например, расходы из таблицы). Факты из 1С так не меняются. "
             "Только базовые показатели, не формулы. Один вызов — один показатель одной компании.",
             _obj({"company": COMPANY_EXACT,
                   "metric": {"type": "string", "description": "Код базового показателя"},
                   "values": {"type": "object", "description": "{\"ГГГГ-ММ\": число, …} — в единицах показателя "
                                                               "(₽ — в рублях, не в тыс. и не в млн)",
                              "additionalProperties": {"type": "number"}},
                   "source_note": {"type": "string", "description": "Откуда: имя файла и лист"}},
                  ["company", "metric", "values"]),
             op="facts.set_facts", owner_only=True),
    ToolSpec("create_card", "Подготовить новую карточку канбана.",
             _obj({"company": COMPANY_EXACT, "department": DEPARTMENT, "title": {"type": "string"},
                   "description": {"type": "string"}, "assignee_id": {"type": "integer"},
                   "priority": PRIORITY, "due_date": {"type": "string", "description": "ГГГГ-ММ-ДД"},
                   "status": CARD_STATUS}, ["company", "department", "title"]),
             op="kanban.create_card"),
    ToolSpec("move_card", "Подготовить перемещение карточки в другую колонку (или на другую позицию).",
             _obj({"card_id": {"type": "integer"}, "status": CARD_STATUS,
                   "position": {"type": "integer", "description": "0 — наверх колонки; не указывать — в конец"}},
                  ["card_id", "status"]),
             op="kanban.move_card"),
    ToolSpec("update_card", "Подготовить изменение полей карточки. Передавай только меняющиеся поля; null — очистить.",
             _obj({"card_id": {"type": "integer"}, "title": {"type": "string"},
                   "description": {"type": ["string", "null"]}, "assignee_id": {"type": ["integer", "null"]},
                   "priority": PRIORITY, "due_date": {"type": ["string", "null"]}}, ["card_id"]),
             op="kanban.update_card"),
    ToolSpec("delete_card", "Подготовить удаление карточки.", _obj({"card_id": {"type": "integer"}}, ["card_id"]),
             op="kanban.delete_card"),
    ToolSpec("create_recurring_task", "Подготовить новую регулярную задачу по расписанию.",
             _obj({"title": {"type": "string"}, "cron": CRON, "department": DEPARTMENT,
                   "company": {"type": ["string", "null"], "description": "Код компании; null — все компании"},
                   "assignee_id": {"type": "integer"}, "description": {"type": "string"},
                   "is_active": {"type": "boolean"}}, ["title", "cron", "department"]),
             op="recurring.create_task", owner_only=True),
    ToolSpec("update_recurring_task", "Подготовить изменение регулярной задачи (только меняющиеся поля).",
             _obj({"task_id": {"type": "integer"}, "title": {"type": "string"}, "cron": CRON,
                   "department": DEPARTMENT, "company": {"type": ["string", "null"]},
                   "assignee_id": {"type": ["integer", "null"]}, "description": {"type": ["string", "null"]},
                   "is_active": {"type": "boolean"}}, ["task_id"]),
             op="recurring.update_task", owner_only=True),
    ToolSpec("delete_recurring_task", "Подготовить удаление регулярной задачи.",
             _obj({"task_id": {"type": "integer"}}, ["task_id"]), op="recurring.delete_task", owner_only=True),
    ToolSpec("create_employee", "Подготовить добавление сотрудника (доступ к сайту и ботам по Telegram ID).",
             _obj({"telegram_id": {"type": "integer"}, "full_name": {"type": "string"},
                   "role": {"type": "string", "enum": ["head", "employee"]}, "department": DEPARTMENT,
                   "company": {"type": ["string", "null"], "description": "Код компании; null — все компании"},
                   "username": {"type": "string"}}, ["telegram_id", "full_name", "department"]),
             op="employees.create", owner_only=True),
    ToolSpec("update_employee", "Подготовить изменение сотрудника: роль, отдел, компания, отключение доступа (is_active=false).",
             _obj({"employee_id": {"type": "integer"}, "full_name": {"type": "string"},
                   "role": {"type": "string", "enum": ["head", "employee"]}, "department": DEPARTMENT,
                   "company": {"type": ["string", "null"]}, "is_active": {"type": "boolean"},
                   "username": {"type": ["string", "null"]}}, ["employee_id"]),
             op="employees.update", owner_only=True),
    ToolSpec("grant_access", "Подготовить выдачу доступа к чужому отделу (чтение; can_write_kanban — ещё и канбан).",
             _obj({"employee_id": {"type": "integer"}, "department": DEPARTMENT,
                   "company": {"type": ["string", "null"], "description": "null — все компании"},
                   "can_write_kanban": {"type": "boolean"}}, ["employee_id", "department"]),
             op="employees.grant_access", owner_only=True),
    ToolSpec("revoke_access", "Подготовить отзыв дополнительного доступа (access_id из list_employees).",
             _obj({"access_id": {"type": "integer"}}, ["access_id"]), op="employees.revoke_access", owner_only=True),
    ToolSpec("save_page", "Подготовить создание страницы сайта или полную замену её блоков. "
             "Передавай весь список блоков целиком — он заменит текущий.",
             _obj({"slug": SLUG, "title": {"type": "string", "description": "Название в меню, коротко: «Сводка», «ОП»"},
                   "blocks": {"type": "array", "items": BLOCK}, "department": PAGE_DEPT,
                   "sort_order": {"type": "integer", "description": "Место в меню: меньше — левее. Новые — в конец"},
                   "in_menu": {"type": "boolean"}}, ["slug", "title", "blocks"]),
             op="site.save_page", owner_only=True),
    ToolSpec("update_page", "Подготовить изменение названия, видимости или места страницы в меню (без блоков).",
             _obj({"slug": SLUG, "title": {"type": "string"}, "department": PAGE_DEPT,
                   "sort_order": {"type": "integer"}, "in_menu": {"type": "boolean"}}, ["slug"]),
             op="site.update_page", owner_only=True),
    ToolSpec("delete_page", "Подготовить удаление страницы сайта (её можно будет вернуть через restore_page).",
             _obj({"slug": SLUG}, ["slug"]), op="site.delete_page", owner_only=True),
    ToolSpec("restore_page", "Подготовить возврат страницы к прошлой версии (номер — из get_page).",
             _obj({"slug": SLUG, "version": {"type": "integer"}}, ["slug", "version"]),
             op="site.restore_page", owner_only=True),
    ToolSpec("update_site_settings", "Подготовить изменение настроек сайта. Передавай только меняющиеся поля; "
             "null — вернуть значение по умолчанию.",
             _obj({"brand_name": {"type": ["string", "null"], "description": "Название в меню, до 40 символов"},
                   "brand_subtitle": {"type": ["string", "null"], "description": "Подзаголовок под названием; '' — убрать"},
                   "brand_color": {"type": ["string", "null"], "description": "Тёмный цвет меню и акцента, #RRGGBB"},
                   "accent_dark": {"type": ["string", "null"], "description": "Светлый акцент для тёмной темы, #RRGGBB"},
                   "default_period": {"type": ["string", "null"], "description": "month, prev_month, quarter, half, ytd "
                                      "или конкретный ГГГГ-ММ..ГГГГ-ММ"},
                   "default_company": {"type": ["string", "null"], "description": "Код компании или holding"}}, []),
             op="site.update_settings", owner_only=True),
    ToolSpec("set_site_css", "Подготовить замену своих стилей сайта (CSS поверх встроенных) — так меняется вид "
             "любого элемента: цвета, размеры, шрифты, отступы, порядок, скрытие (display:none). Передавай ВЕСЬ "
             "CSS целиком: текущий из get_ui_map + правки. Пустая строка — убрать свои стили. Запрещены url(), "
             "@import, image-set, @font-face, обратная косая черта и «<».",
             _obj({"css": {"type": "string"}}, ["css"]), op="site.set_css", owner_only=True),
    ToolSpec("restore_site_css", "Подготовить возврат своих стилей к прошлой версии (1 — предыдущая, см. get_ui_map).",
             _obj({"version": {"type": "integer", "minimum": 1}}, ["version"]), op="site.restore_css", owner_only=True),
    ToolSpec("update_site_texts", "Подготовить замену надписей интерфейса (кнопки, подписи, пояснения). "
             "Ключи — из get_ui_map; null — вернуть текст по умолчанию.",
             _obj({"texts": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}}, ["texts"]),
             op="site.update_texts", owner_only=True),
    ToolSpec("save_service", "Подготовить сохранение подключения к внешнему сервису (создать или изменить). "
             "auth: none, bearer, header (param — имя заголовка), query (param — имя параметра), basic "
             "(secret_user + secret), url ({secret} внутри base_url, вебхуки), google (сервисный аккаунт, "
             "secret — JSON-ключ; base_url можно не задавать). allow_write — разрешить изменения (каждое всё равно с подтверждением).",
             _obj({"name": {"type": "string", "description": "Латиница: amocrm, gsheets"}, "title": {"type": "string"},
                   "base_url": {"type": "string"}, "auth": {"type": "string", "enum": list(connectors.AUTHS)},
                   "secret": {"type": "string", "description": "Имя ключа в .env"},
                   "secret_user": {"type": "string"}, "param": {"type": "string"},
                   "allow_write": {"type": "boolean"}, "notes": {"type": "string"}},
                  ["name", "title", "auth"]), op="services.save", owner_only=True),
    ToolSpec("delete_service", "Подготовить удаление подключения к сервису.",
             _obj({"name": {"type": "string"}}, ["name"]), op="services.delete", owner_only=True),
    ToolSpec("service_write", "Подготовить запрос, который меняет данные во внешнем сервисе (POST/PUT/PATCH/DELETE). "
             "Выполнится только после «Подтвердить»; результат придёт сообщением [бот].",
             _obj({"connection": {"type": "string"}, "method": {"type": "string", "enum": list(connectors.WRITE_METHODS)},
                   "path": {"type": "string"}, "query": {"type": "object"},
                   "body": {"description": "JSON-объект/массив или текст"},
                   "purpose": {"type": "string", "description": "По-человечески: что этот запрос сделает"}},
                  ["connection", "method", "purpose"]), op="services.request", owner_only=True),
    ToolSpec("sheets_write", "Подготовить запись в Google Таблицу (нужно подключение auth=google и доступ «редактор» "
             "у сервисного аккаунта).",
             _obj({"connection": {"type": "string"}, "spreadsheet": {"type": "string"},
                   "range": {"type": "string", "description": "'Лист'!A1 — откуда писать; для append — лист"},
                   "values": {"type": "array", "items": {"type": "array"}},
                   "mode": {"type": "string", "enum": ["update", "append"]}},
                  ["connection", "spreadsheet", "range", "values"]), op="sheets.write", owner_only=True),
    ToolSpec("create_department", "Подготовить новый отдел.",
             _obj({"code": {"type": "string", "description": "Латиница: logistics"}, "name": {"type": "string"},
                   "sort_order": {"type": "integer"}}, ["code", "name"]), op="departments.create", owner_only=True),
    ToolSpec("update_department", "Подготовить переименование отдела или смену места в списке.",
             _obj({"code": DEPARTMENT, "name": {"type": "string"}, "sort_order": {"type": "integer"}}, ["code"]),
             op="departments.update", owner_only=True),
    ToolSpec("delete_department", "Подготовить удаление пустого отдела (без показателей, сотрудников, карточек).",
             _obj({"code": DEPARTMENT}, ["code"]), op="departments.delete", owner_only=True),
    ToolSpec("create_metric", "Подготовить новый показатель. aggregation: sum — суммируется по месяцам "
             "(выручка), avg — среднее, last — на конец периода (остатки), derived — формула из исходных показателей.",
             _obj({"code": {"type": "string", "description": "Латиница и _: gross_sales"}, "name": {"type": "string"},
                   "department": DEPARTMENT, "unit": {"type": "string", "description": "₽, шт, %, дн…"},
                   "aggregation": {"type": "string", "enum": ["sum", "avg", "last", "derived"]},
                   "formula": {"type": "string", "description": "Для derived: revenue / orders, (a - b) / b * 100"},
                   "higher_is_better": {"type": "boolean"}, "is_key": {"type": "boolean", "description": "Показывать в сводке"},
                   "is_visible": {"type": "boolean"}, "decimals": {"type": "integer", "minimum": 0, "maximum": 4},
                   "sort_order": {"type": "integer"}}, ["code", "name", "department", "unit", "aggregation"]),
             op="metrics.create", owner_only=True),
    ToolSpec("update_metric", "Подготовить изменение показателя (только меняющиеся поля). Скрыть — is_visible=false.",
             _obj({"code": {"type": "string"}, "name": {"type": "string"}, "department": DEPARTMENT,
                   "unit": {"type": "string"}, "aggregation": {"type": "string", "enum": ["sum", "avg", "last", "derived"]},
                   "formula": {"type": ["string", "null"]}, "higher_is_better": {"type": "boolean"},
                   "is_key": {"type": "boolean"}, "is_visible": {"type": "boolean"},
                   "decimals": {"type": "integer", "minimum": 0, "maximum": 4}, "sort_order": {"type": "integer"}},
                  ["code"]), op="metrics.update", owner_only=True),
    ToolSpec("delete_metric", "Подготовить удаление показателя (только если по нему нет данных и он нигде не используется).",
             _obj({"code": {"type": "string"}}, ["code"]), op="metrics.delete", owner_only=True),
    ToolSpec("create_company", "Подготовить добавление компании в холдинг.",
             _obj({"code": {"type": "string", "description": "Короткий код латиницей: alpha, beta-shop"},
                   "name": {"type": "string", "description": "Название, как показывать на сайте"},
                   "sort_order": {"type": "integer", "description": "Место в переключателе; по умолчанию в конец"}},
                  ["code", "name"]), op="companies.create", owner_only=True),
    ToolSpec("update_company", "Подготовить переименование компании или смену её места в списке.",
             _obj({"code": COMPANY_EXACT, "name": {"type": "string"}, "sort_order": {"type": "integer"}}, ["code"]),
             op="companies.update", owner_only=True),
    ToolSpec("delete_company", "Подготовить удаление компании (только если по ней ещё нет данных).",
             _obj({"code": COMPANY_EXACT}, ["code"]), op="companies.delete", owner_only=True),
    ToolSpec("onec_save_source", "Подготовить сохранение источника факта в 1С для показателя и компании "
             "(создание или замена). Только базовые показатели, не формульные.",
             _obj({"metric": {"type": "string", "description": "Код базового показателя"}, "company": COMPANY_EXACT,
                   "connection": CONN, **SOURCE_PROPS,
                   "note": {"type": "string", "description": "Коротко для людей: откуда и почему так"},
                   "is_active": {"type": "boolean"}},
                  ["metric", "company", "connection", "entity", "period_mode"]),
             op="onec.save_source", owner_only=True),
    ToolSpec("onec_delete_source", "Подготовить удаление источника в 1С (загруженные факты останутся).",
             _obj({"metric": {"type": "string"}, "company": COMPANY_EXACT}, ["metric", "company"]),
             op="onec.delete_source", owner_only=True),
    ToolSpec("onec_load_facts", "Подготовить загрузку фактов из 1С по настроенным источникам. Без period — "
             "прошлый и текущий месяц (так же работает автозагрузка). Для истории — например half.",
             _obj({"period": PERIOD, "metrics": {"type": "array", "items": {"type": "string"}},
                   "company": COMPANY}, []),
             op="onec.load_facts", owner_only=True),
]

ALL_TOOLS = {t.name: t for t in READ_TOOLS + WRITE_TOOLS}


def tools_for(actor: Actor, allow_plans: bool = True) -> list[ToolSpec]:
    """Набор инструментов для бота. Неположенные инструменты не показываем модели вовсе."""
    out = [t for t in ALL_TOOLS.values() if actor.is_owner or not t.owner_only]
    if not allow_plans:
        out = [t for t in out if t.op != "plans.set_plan"]
    return out


# ---------- сериализация ----------


def _num(v: float | None, decimals: int = 2) -> float | int | None:
    if v is None:
        return None
    r = round(v, decimals)
    return int(r) if r == int(r) else r


def _row(r: metrics.MetricRow, with_series: bool = True) -> dict:
    d = {
        "code": r.code, "name": r.name, "unit": r.unit,
        "plan": _num(r.plan), "fact": _num(r.fact), "deviation": _num(r.deviation),
        "completion_pct": _num(r.completion, 1),
        "higher_is_better": r.higher_is_better,
    }
    if with_series and len(r.series) > 1:
        d["by_month"] = [{"month": p.period.strftime("%Y-%m"), "plan": _num(p.plan), "fact": _num(p.fact)}
                         for p in r.series]
    return d


async def _names(session: AsyncSession, model, ids) -> dict[int, str]:
    ids = {i for i in ids if i is not None}
    out = {}
    for i in ids:
        obj = await session.get(model, i)
        out[i] = (getattr(obj, "full_name", None) or obj.name) if obj else f"#{i}"
    return out


def _period(spec: str) -> Period:
    return Period.parse(spec, today_local())


# ---------- выполнение чтения ----------


async def run_read(session: AsyncSession, actor: Actor, name: str, args: dict[str, Any]) -> Any:
    if name == "get_summary":
        p = _period(args["period"])
        rows = await metrics.summary_metrics(session, actor, args["company"], p)
        fr = await sync.freshness(session, actor, args["company"])
        return {"period": p.label, "freshness": fr.label, "metrics": [_row(r, False) | {"department": r.department} for r in rows]}

    if name == "get_department_metrics":
        p = _period(args["period"])
        rows = await metrics.department_metrics(session, actor, args["company"], args["department"], p)
        fr = await sync.freshness(session, actor, args["company"], args["department"])
        return {"period": p.label, "freshness": fr.label, "metrics": [_row(r) for r in rows]}

    if name == "get_metric":
        p = _period(args["period"])
        return {"period": p.label, **_row(await metrics.metric(session, actor, args["company"], args["metric"], p))}

    if name == "get_freshness":
        f = await sync.freshness(session, actor, args["company"], args.get("department"))
        return {"label": f.label, "stale": f.is_stale, "error": f.message}

    if name == "get_kanban":
        board = await kanban.board(session, actor, args["company"], args["department"])
        cards = [c for col in board.values() for c in col]
        emp = await _names(session, Employee, [c.assignee_id for c in cards])
        comp = await _names(session, Company, [c.company_id for c in cards])
        return {
            STATUS_LABELS[st]: [
                {"id": c.id, "title": c.title, "company": comp[c.company_id],
                 "assignee": emp.get(c.assignee_id), "assignee_id": c.assignee_id,
                 "priority": PRIORITY_LABELS[c.priority],
                 "due": c.due_date.isoformat() if c.due_date else None,
                 "description": c.description}
                for c in col
            ]
            for st, col in board.items()
        }

    if name == "list_employees":
        emps = await employees.list_employees(session, actor, args.get("department"), bool(args.get("include_inactive")))
        depts = {d.id: d.code for d in await refs.list_departments(session)}
        comps = {c.id: c.code for c in await refs.list_companies(session)}
        out = []
        for e in emps:
            item = {"id": e.id, "name": e.full_name, "role": e.role.value,
                    "department": depts.get(e.department_id), "company": comps.get(e.company_id, "все"),
                    "active": e.is_active}
            if actor.is_owner:
                item["telegram_id"] = e.telegram_id
                item["extra_access"] = [
                    {"access_id": a.id, "department": depts[a.department_id],
                     "company": comps.get(a.company_id, "все"), "can_write_kanban": a.can_write_kanban}
                    for a in await employees.list_access(session, actor, e.id)
                ]
            out.append(item)
        return out

    if name == "list_recurring_tasks":
        tasks = await recurring.list_tasks(session, actor, args.get("department"))
        emp = await _names(session, Employee, [t.assignee_id for t in tasks])
        depts = await _names(session, Department, [t.department_id for t in tasks])
        comps = await _names(session, Company, [t.company_id for t in tasks])
        return [{"id": t.id, "title": t.title, "cron": t.cron, "department": depts[t.department_id],
                 "company": comps.get(t.company_id, "все"), "assignee": emp.get(t.assignee_id),
                 "active": t.is_active, "next_run": fmt_dt(t.next_run_at)} for t in tasks]

    if name == "get_change_log":
        rows = await changelog.recent(session, actor, args.get("limit", 20), args.get("entity"))
        return [{"when": fmt_dt(r.ts), "who": r.actor_name, "channel": r.channel.value,
                 "what": r.summary or f"{r.entity} {r.action}"} for r in rows]

    if name == "list_pages":
        out = []
        for p in await site.list_pages(session, actor):
            out.append({"slug": p.slug, "title": p.title, "department": p.department, "sort_order": p.sort_order,
                        "in_menu": p.in_menu, "version": p.version,
                        "blocks": await site.describe_blocks(session, p.blocks)})
        return out

    if name == "get_page":
        p = await site.get_page(session, actor, args["slug"])
        versions = await site.list_versions(session, actor, p.slug)
        return {"slug": p.slug, "title": p.title, "department": p.department, "sort_order": p.sort_order,
                "in_menu": p.in_menu, "version": p.version, "blocks": p.blocks,
                "previous_versions": [{"version": v.version, "title": v.title, "saved": fmt_dt(v.created_at)}
                                      for v in versions]}

    if name == "get_site_settings":
        if not actor.is_owner:
            raise ServiceError("Настройки сайта видит только владелец")
        cfg = await sitecfg.get_all(session)
        return {k: v for k, v in cfg.items() if k not in ("custom_css", "texts")}
    if name == "get_ui_map":
        if not actor.is_owner:
            raise ServiceError("Настройки сайта видит только владелец")
        cfg = await sitecfg.get_all(session)
        over = cfg["texts"] or {}
        return {"elements": sitecfg.UI_MAP,
                "texts": [{"key": k, "default": v, **({"current": over[k]} if k in over else {})}
                          for k, v in sitecfg.TEXTS.items()],
                "custom_css": cfg["custom_css"],
                "css_versions": [{"version": i, "saved": h["saved"], "length": len(h["css"])}
                                 for i, h in enumerate(await sitecfg.css_history(session), 1)],
                "notes": "Свои стили подключаются после встроенных, поэтому обычного селектора хватает. "
                         "Тёмная тема: :root[data-theme=dark] и @media (prefers-color-scheme: dark) "
                         "{ :root:not([data-theme=light]) {…} }. Телефон: @media (max-width: 899px)."}
    if name == "list_services":
        reqs = await secrets.pending_requests(session, actor)
        return {"connections": await connectors.list_connections(session, actor),
                "env_keys": secrets.names(),
                "waiting_for_owner": [{"key": r.key, "title": r.title} for r in reqs]}
    if name == "request_secret":
        from app.web.app import site_url

        req = await secrets.create_request(session, actor, key=args["key"], title=args["title"],
                                           instructions=args.get("instructions"), kind=args.get("kind", "text"))
        return {"link": f"{site_url()}/secret/{req.public_id}", "key": req.key,
                "note": "Отправь владельцу ссылку и инструкцию. Ссылка открывается только у владельца после входа "
                        "на сайт (/site), действует 24 часа. Значение придёт в .env, тебе — сообщение [бот]."}
    if name == "read_file":
        from app.agent import uploads

        return uploads.read(args["file_id"], args.get("sheet"), args.get("start_row", 1), args.get("rows", 400))
    if name == "onec_request_connection":
        from app.web.app import site_url

        req = await secrets.create_onec_request(session, actor, name=args["name"], url=args.get("url"),
                                                user=args.get("user"), instructions=args.get("instructions"))
        return {"link": f"{site_url()}/secret/{req.public_id}", "connection": req.key.removeprefix("ONEC_").lower(),
                "note": "Отправь владельцу ссылку. Открывается только у владельца после входа на сайт (/site), "
                        "действует 24 часа. Сайт проверит связь перед сохранением; тебе придёт сообщение [бот]."}
    if name == "service_request":
        return await connectors.request_read(session, actor, connection=args["connection"], path=args.get("path", ""),
                                             query=args.get("query"), method=args.get("method", "GET"))
    if name == "sheets_read":
        return await connectors.sheets_read(session, actor, spreadsheet=args["spreadsheet"], range=args.get("range"),
                                            connection=args.get("connection"))
    if name == "onec_connections":
        conns = await onec.connections(actor)
        return conns or {"connections": [], "note": "Баз 1С ещё нет — подключи через onec_request_connection"}
    if name == "onec_find_objects":
        return await onec.find_objects(actor, args["connection"], args.get("search"), args.get("kind"))
    if name == "onec_describe":
        return await onec.describe(actor, args["connection"], args["entity"])
    if name == "onec_query":
        return await onec.query(actor, args["connection"], args["entity"], args.get("select"), args.get("filter"),
                                args.get("top", 10), args.get("orderby"), args.get("expand"))
    if name == "onec_test_source":
        return await onec.test_source(actor, **args)
    if name == "onec_list_sources":
        return await onec.list_sources(session, actor) or {"sources": [], "note": "Источники ещё не настроены"}

    raise ServiceError(f"Неизвестный инструмент {name}")


# ---------- общий вход ----------


MAX_RESULT = 40_000


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    preview: changes.ChangePreview | None = None  # для записи — что показать с кнопкой


async def execute(session: AsyncSession, actor: Actor, name: str, args: dict[str, Any],
                  allowed: dict[str, ToolSpec], sandbox: Any = None) -> ToolOutcome:
    """sandbox — песочница хода (services/sandbox.py): изменения хода проверяются цепочкой,
    каждое видит предыдущие (отдел → его показатели → страница)."""
    spec = allowed.get(name)
    if spec is None:
        return ToolOutcome(f"Инструмент {name} недоступен", is_error=True)
    try:
        if spec.is_write:
            if sandbox is not None:
                p = await sandbox.step(actor, spec.op, args)
            else:
                p = await changes.preview(session, actor, spec.op, args)
            return ToolOutcome(
                "Изменение НЕ применено. Пользователю показан предпросмотр с кнопками «Подтвердить»/«Отмена». "
                "Следующие изменения этого хода уже учитывают его (одно подтверждение на всё):\n"
                + p.render(),
                preview=p,
            )
        result = await run_read(session, actor, name, args)
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + f"… [обрезано: ответ длиннее {MAX_RESULT} символов — сузьте запрос]"
        return ToolOutcome(text)
    except ServiceError as e:
        return ToolOutcome(f"Ошибка: {e}", is_error=True)
    except (KeyError, TypeError, ValueError) as e:
        return ToolOutcome(f"Ошибка во входных параметрах: {e}", is_error=True)
