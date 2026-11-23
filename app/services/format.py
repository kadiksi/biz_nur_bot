from datetime import date, datetime

from app.timeutil import to_local

NBSP = " "

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def fmt_number(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return "—"
    s = f"{value:,.{decimals}f}"
    return s.replace(",", NBSP).replace(".", ",")


def fmt_value(value: float | None, unit: str, decimals: int = 0) -> str:
    if value is None:
        return "—"
    return f"{fmt_number(value, decimals)}{NBSP}{unit}"


def month_label(d: date) -> str:
    return f"{MONTHS_NOM[d.month - 1]} {d.year}"


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_dt(dt: datetime | None) -> str:
    """naive UTC из БД → местное время."""
    return to_local(dt).strftime("%d.%m.%Y %H:%M") if dt else "—"
