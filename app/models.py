"""Схема БД. Читать и писать её напрямую можно только из app/services и скриптов загрузки."""
from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Role(enum.StrEnum):
    OWNER = "owner"
    HEAD = "head"
    EMPLOYEE = "employee"


class CardStatus(enum.StrEnum):
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"


class Aggregation(enum.StrEnum):
    SUM = "sum"          # суммируется по месяцам и компаниям
    AVG = "avg"          # среднее по месяцам и компаниям
    LAST = "last"        # на конец периода (остатки); по компаниям — сумма
    DERIVED = "derived"  # считается формулой из других метрик, не хранится


class Channel(enum.StrEnum):
    SITE = "site"
    MAIN_BOT = "main_bot"
    DEPT_BOT = "dept_bot"
    SCHEDULER = "scheduler"
    SCRIPT = "script"


class SyncState(enum.StrEnum):
    OK = "ok"
    ERROR = "error"
    RUNNING = "running"


def _enum(e: type[enum.Enum]) -> Enum:
    return Enum(e, native_enum=False, values_callable=lambda x: [m.value for m in x], length=20)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.EMPLOYEE)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    # None — сотрудник работает на все компании холдинга
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmployeeAccess(Base):
    """Дополнительный доступ сверх своего отдела."""

    __tablename__ = "employee_access"
    __table_args__ = (UniqueConstraint("employee_id", "department_id", "company_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))  # None — все компании
    can_write_kanban: Mapped[bool] = mapped_column(Boolean, default=False)


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    unit: Mapped[str] = mapped_column(String(16))
    aggregation: Mapped[Aggregation] = mapped_column(_enum(Aggregation))
    formula: Mapped[str | None] = mapped_column(String(300))
    higher_is_better: Mapped[bool] = mapped_column(Boolean, default=True)
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)       # показывать в Сводке
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True)    # False — только база для формул
    decimals: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MetricValue(Base):
    __tablename__ = "metric_values"
    __table_args__ = (UniqueConstraint("company_id", "metric_id", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    metric_id: Mapped[int] = mapped_column(ForeignKey("metric_definitions.id"))
    period: Mapped[date] = mapped_column(Date)  # первое число месяца
    plan: Mapped[float | None] = mapped_column(Float)
    fact: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # demo | 1c | manual
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class KanbanCard(Base):
    __tablename__ = "kanban_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CardStatus] = mapped_column(_enum(CardStatus), default=CardStatus.BACKLOG)
    position: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # low | normal | high
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    recurring_task_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_tasks.id", ondelete="SET NULL"))
    due_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RecurringTask(Base):
    __tablename__ = "recurring_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    cron: Mapped[str] = mapped_column(String(100))  # crontab, в settings.timezone
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncStatus(Base):
    __tablename__ = "sync_status"
    __table_args__ = (UniqueConstraint("source", "company_id", "department_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))  # 1c | demo
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    status: Mapped[SyncState] = mapped_column(_enum(SyncState), default=SyncState.OK)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    message: Mapped[str | None] = mapped_column(Text)


class OneCSource(Base):
    """Откуда в 1С брать факт показателя для компании. Настраивает главный бот."""

    __tablename__ = "onec_sources"
    __table_args__ = (UniqueConstraint("metric_id", "company_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metric_definitions.id"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    connection: Mapped[str] = mapped_column(String(32))     # имя подключения из .env (ONEC_<ИМЯ>_URL)
    entity: Mapped[str] = mapped_column(String(500))        # Document_…, AccumulationRegister_…/Turnovers
    field: Mapped[str | None] = mapped_column(String(200))  # что суммировать; None — считать строки
    aggregation: Mapped[str] = mapped_column(String(8), default="sum")  # sum | count
    filter: Mapped[str | None] = mapped_column(Text)        # OData $filter
    period_mode: Mapped[str] = mapped_column(String(12), default="turnovers")
    date_field: Mapped[str | None] = mapped_column(String(100))
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SiteSetting(Base):
    """Настройки сайта (название, цвета, период по умолчанию). Меняет владелец через бота."""

    __tablename__ = "site_settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SecretRequest(Base):
    """Запрос ключа/токена у владельца. Значение вводится на сайте и пишется прямо в .env — боту не показывается."""

    __tablename__ = "secret_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    key: Mapped[str] = mapped_column(String(64))            # имя переменной в .env
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str | None] = mapped_column(Text)  # где взять: шаги для владельца
    kind: Mapped[str] = mapped_column(String(10), default="text")  # text | json | onec
    defaults: Mapped[str | None] = mapped_column(Text)  # onec: JSON с подсказками формы (адрес, логин), без паролей
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime)


class ServiceConnection(Base):
    """Подключение к внешнему сервису (API). Бот ходит только по зарегистрированным адресам."""

    __tablename__ = "service_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(500))
    auth: Mapped[str] = mapped_column(String(10), default="none")  # none|bearer|header|query|basic|url|google
    secret: Mapped[str | None] = mapped_column(String(64))         # имя ключа в .env
    secret_user: Mapped[str | None] = mapped_column(String(64))    # для basic: имя пользователя (ключ в .env)
    param: Mapped[str | None] = mapped_column(String(64))          # имя заголовка или параметра
    allow_write: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChangeLog(Base):
    __tablename__ = "change_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    actor_name: Mapped[str] = mapped_column(String(200))
    channel: Mapped[Channel] = mapped_column(_enum(Channel))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(50))
    summary: Mapped[str | None] = mapped_column(Text)  # человекочитаемо, как в подтверждении бота
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SitePage(Base):
    """Страница сайта. Состав (blocks) задаёт главный бот; цифры подставляются при показе."""

    __tablename__ = "site_pages"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    # None — общая страница (видна всем, блоки проверяют доступ сами); иначе — только тем, кто видит отдел
    department: Mapped[str | None] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    in_menu: Mapped[bool] = mapped_column(Boolean, default=True)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SitePageVersion(Base):
    """Прошлые версии страницы — для отката."""

    __tablename__ = "site_page_versions"
    __table_args__ = (UniqueConstraint("slug", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(100))
    department: Mapped[str | None] = mapped_column(String(32))
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebToken(Base):
    """Одноразовая ссылка для входа (kind=login) и сессия браузера (kind=session). Храним только хеш."""

    __tablename__ = "web_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(10))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
