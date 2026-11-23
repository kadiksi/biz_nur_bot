"""Периоды. Данные хранятся по месяцам; период может начинаться и кончаться внутри месяца.

Форматы: month | prev_month | quarter | half | ytd | YYYY-MM | YYYY-MM..YYYY-MM | YYYY-MM-DD..YYYY-MM-DD
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.services.errors import ValidationError
from app.services.format import MONTHS_NOM, month_label
from app.timeutil import to_local, utcnow

MAX_MONTHS = 36
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

PRESETS = {
    "month": "Текущий месяц",
    "prev_month": "Прошлый месяц",
    "quarter": "Текущий квартал",
    "half": "Последние 6 месяцев",
    "ytd": "С начала года",
}


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + d.month - 1 + n
    return date(idx // 12, idx % 12 + 1, 1)


def month_start(d: date) -> date:
    return d.replace(day=1)


def parse_month(value: str | date) -> date:
    if isinstance(value, date):
        return month_start(value)
    m = _MONTH_RE.match(value.strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValidationError(f"Месяц «{value}» не распознан, нужен формат ГГГГ-ММ")
    return date(int(m.group(1)), int(m.group(2)), 1)


def month_end(d: date) -> date:
    return add_months(month_start(d), 1) - timedelta(days=1)


def parse_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    m = _DAY_RE.match(value.strip())
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except (AttributeError, ValueError):
        raise ValidationError(f"Дата «{value}» не распознана, нужен формат ГГГГ-ММ-ДД") from None


def today_local() -> date:
    return to_local(utcnow()).date()


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # первое число последнего месяца, включительно
    day_from: date | None = None  # точные даты, если период начинается/кончается внутри месяца
    day_to: date | None = None

    def __post_init__(self):
        if self.start.day != 1 or self.end.day != 1:
            raise ValidationError("start и end — первые числа месяцев")
        if self.start > self.end:
            raise ValidationError("Начало периода позже конца")
        if len(self.months()) > MAX_MONTHS:
            raise ValidationError(f"Период длиннее {MAX_MONTHS} месяцев")
        if self.date_from > self.date_to:
            raise ValidationError("Дата начала позже даты конца")
        if month_start(self.date_from) != self.start or month_start(self.date_to) != self.end:
            raise ValidationError("Даты не совпадают с месяцами периода")

    @property
    def date_from(self) -> date:
        return self.day_from or self.start

    @property
    def date_to(self) -> date:
        return self.day_to or month_end(self.end)

    @property
    def is_partial(self) -> bool:
        """Начинается или кончается не на границе месяца."""
        return self.date_from != self.start or self.date_to != month_end(self.end)

    def month_range(self, mo: date) -> tuple[date, date]:
        """Какие дни месяца mo входят в период."""
        return max(mo, self.date_from), min(month_end(mo), self.date_to)

    def coverage(self, mo: date) -> float:
        """Доля дней месяца mo внутри периода (1 — месяц целиком)."""
        a, b = self.month_range(mo)
        return ((b - a).days + 1) / month_end(mo).day

    @classmethod
    def days(cls, date_from: str | date, date_to: str | date) -> Period:
        a, b = parse_day(date_from), parse_day(date_to)
        if a > b:
            raise ValidationError("Дата начала позже даты конца")
        p = cls(month_start(a), month_start(b), a, b)
        if not p.is_partial:  # целые месяцы — обычный период
            return cls(p.start, p.end)
        return p

    @property
    def spec(self) -> str:
        """Строка, из которой Period.parse соберёт тот же период."""
        if self.is_partial:
            return f"{self.date_from:%Y-%m-%d}..{self.date_to:%Y-%m-%d}"
        if self.start == self.end:
            return f"{self.start:%Y-%m}"
        return f"{self.start:%Y-%m}..{self.end:%Y-%m}"

    def months(self) -> list[date]:
        out, d = [], self.start
        while d <= self.end:
            out.append(d)
            d = add_months(d, 1)
        return out

    @property
    def label(self) -> str:
        if self.is_partial:
            a, b = self.date_from, self.date_to
            if a == b:
                return f"{a.day} {MONTHS_GEN[a.month - 1]} {a.year}"
            left = f"{a.day}" if (a.year, a.month) == (b.year, b.month) else (
                f"{a.day} {MONTHS_GEN[a.month - 1]}" + (f" {a.year}" if a.year != b.year else ""))
            return f"{left} – {b.day} {MONTHS_GEN[b.month - 1]} {b.year}"
        if self.start == self.end:
            return month_label(self.start)
        if self.start.year == self.end.year:
            return f"{MONTHS_NOM[self.start.month - 1]}–{MONTHS_NOM[self.end.month - 1]} {self.end.year}"
        return f"{month_label(self.start)} – {month_label(self.end)}"

    @classmethod
    def month(cls, d: date) -> Period:
        d = month_start(d)
        return cls(d, d)

    @classmethod
    def parse(cls, spec: str, today: date | None = None) -> Period:
        today = today or today_local()
        cur = month_start(today)
        spec = (spec or "month").strip()
        if spec == "month":
            return cls(cur, cur)
        if spec == "prev_month":
            p = add_months(cur, -1)
            return cls(p, p)
        if spec == "quarter":
            return cls(date(cur.year, (cur.month - 1) // 3 * 3 + 1, 1), cur)
        if spec == "half":
            return cls(add_months(cur, -5), cur)
        if spec == "ytd":
            return cls(date(cur.year, 1, 1), cur)
        if ".." in spec:
            a, b = (x.strip() for x in spec.split("..", 1))
            if _DAY_RE.match(a) or _DAY_RE.match(b):
                a = a if _DAY_RE.match(a) else f"{parse_month(a):%Y-%m-%d}"
                b = b if _DAY_RE.match(b) else f"{month_end(parse_month(b)):%Y-%m-%d}"
                return cls.days(a, b)
            return cls(parse_month(a), parse_month(b))
        if _DAY_RE.match(spec):
            return cls.days(spec, spec)
        if _MONTH_RE.match(spec):
            m = parse_month(spec)
            return cls(m, m)
        raise ValidationError(f"Период «{spec}» не распознан")
