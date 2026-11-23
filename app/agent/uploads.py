"""Файлы, которые владелец присылает боту: Excel, CSV, текст — разбираем в таблицы;
PDF и картинки отдаём Claude как есть (он их читает сам).

Файлы держим в памяти бота сутки: после перезапуска их нужно прислать заново.
"""
from __future__ import annotations

import base64
import csv
import io
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import PurePath

from app.services.errors import NotFound, ValidationError
from app.timeutil import utcnow

MAX_BYTES = 15 * 1024 * 1024
MAX_ROWS = 50_000          # строк на лист
MAX_COLS = 200
OVERVIEW_ROWS = 60         # сколько строк каждого листа показать сразу
OVERVIEW_CHARS = 25_000
READ_ROWS = 400            # за один вызов read_file
TTL = timedelta(hours=24)

TABLES = {".xlsx", ".xlsm", ".csv", ".tsv"}
TEXTS = {".txt", ".json", ".md", ".xml"}
IMAGES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
          ".gif": "image/gif"}
PDF = ".pdf"
SUPPORTED = "Excel (.xlsx), CSV, PDF, фото и картинки, текстовые файлы"


@dataclass
class Upload:
    id: str
    name: str
    sheets: dict[str, list[list]] = field(default_factory=dict)  # для таблиц
    text: str | None = None
    created_at: datetime = field(default_factory=utcnow)


_store: dict[str, Upload] = {}


def _gc() -> None:
    for k in [k for k, u in _store.items() if utcnow() - u.created_at > TTL]:
        del _store[k]


def get(file_id: str) -> Upload:
    _gc()
    u = _store.get(file_id)
    if u is None:
        raise NotFound("Файл не найден — бота перезапускали или прошли сутки. Попросите прислать его ещё раз")
    return u


def _cell(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0) else v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, float):
        return int(v) if v.is_integer() else round(v, 6)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _trim(rows: list[list]) -> list[list]:
    # без пустых строк в конце и пустых колонок справа
    while rows and all(c is None for c in rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, c in enumerate(r) if c is not None), default=0) for r in rows), default=0)
    return [r[:width] + [None] * (width - len(r[:width])) for r in rows]


def _xlsx(data: bytes) -> dict[str, list[list]]:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)  # формулы — посчитанные значения
    except Exception as e:  # noqa: BLE001 — битый или не тот формат
        raise ValidationError(f"Не получилось открыть Excel-файл: {e}") from None
    sheets = {}
    try:
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True, max_col=MAX_COLS)):
                if i >= MAX_ROWS:
                    break
                rows.append([_cell(c) for c in row])
            sheets[ws.title] = _trim(rows)
    finally:
        wb.close()
    return sheets


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _num(s: str | None):
    if s is None:
        return None
    t = s.replace(" ", "").replace(" ", "")
    if t.count(",") == 1 and "." not in t:
        t = t.replace(",", ".")
    try:
        f = float(t)
    except ValueError:
        return s
    return int(f) if f.is_integer() and "." not in t else f


def _csv(data: bytes, suffix: str) -> dict[str, list[list]]:
    text = _decode(data)
    delim = "\t" if suffix == ".tsv" else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|").delimiter
        except csv.Error:
            delim = ";" if text[:5000].count(";") > text[:5000].count(",") else ","
    rows = [[_num(_cell(c)) for c in r[:MAX_COLS]] for _, r in zip(range(MAX_ROWS), csv.reader(io.StringIO(text), delimiter=delim))]
    return {"CSV": _trim(rows)}


def add(name: str, data: bytes) -> tuple[Upload | None, list[dict]]:
    """Разобрать файл. Возвращает (upload для таблиц/текста или None, блоки для Claude)."""
    if len(data) > MAX_BYTES:
        raise ValidationError(f"Файл больше {MAX_BYTES // 1024 // 1024} МБ — пришлите поменьше")
    suffix = PurePath(name or "").suffix.lower()
    if suffix in IMAGES:
        return None, [{"type": "image", "source": {"type": "base64", "media_type": IMAGES[suffix],
                                                    "data": base64.b64encode(data).decode()}}]
    if suffix == PDF:
        return None, [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                       "data": base64.b64encode(data).decode()}, "title": name}]
    if suffix == ".xls":
        raise ValidationError("Старый формат .xls я не читаю. Откройте файл в Excel и сохраните как .xlsx")
    if suffix in TABLES:
        sheets = _xlsx(data) if suffix in (".xlsx", ".xlsm") else _csv(data, suffix)
        up = Upload(secrets.token_hex(4), name, sheets=sheets)
    elif suffix in TEXTS:
        up = Upload(secrets.token_hex(4), name, text=_decode(data))
    else:
        raise ValidationError(f"Такой файл я не читаю. Подходят: {SUPPORTED}")
    _gc()
    _store[up.id] = up
    return up, [{"type": "text", "text": overview(up)}]


def add_text(name: str, text: str) -> tuple[Upload, list[dict]]:
    """Готовый текст (расшифровка записи) как присланный файл."""
    up = Upload(secrets.token_hex(4), name, text=text)
    _gc()
    _store[up.id] = up
    return up, [{"type": "text", "text": overview(up)}]


def _fmt_row(n: int, row: list) -> str:
    return f"{n}: " + " | ".join("" if c is None else str(c) for c in row)


def overview(up: Upload) -> str:
    head = f"[файл «{up.name}», file_id={up.id}]"
    if up.text is not None:
        body = up.text[:OVERVIEW_CHARS]
        more = f"\n… показано {len(body)} из {len(up.text)} символов, дальше — read_file со start_row={len(body) // 1000 + 1}" if len(up.text) > len(body) else ""
        return f"{head}\n{body}{more}"
    parts = [head]
    budget = OVERVIEW_CHARS
    for name, rows in up.sheets.items():
        width = len(rows[0]) if rows else 0
        lines = [f"\nЛист «{name}»: {len(rows)} строк × {width} колонок"]
        for i, r in enumerate(rows[:OVERVIEW_ROWS], 1):
            lines.append(_fmt_row(i, r))
        if len(rows) > OVERVIEW_ROWS:
            lines.append(f"… ещё {len(rows) - OVERVIEW_ROWS} строк: read_file(file_id, sheet, start_row)")
        chunk = "\n".join(lines)
        if len(chunk) > budget:
            chunk = chunk[:budget] + "\n… обрезано, остальное — read_file"
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            parts.append("… остальные листы — read_file")
            break
    return "\n".join(parts)


def read(file_id: str, sheet: str | None = None, start_row: int = 1, rows: int = READ_ROWS) -> dict:
    up = get(file_id)
    if up.text is not None:
        start = max(0, (start_row - 1) * 1000)
        return {"file": up.name, "text": up.text[start:start + 30_000], "total_chars": len(up.text)}
    if sheet is None:
        sheet = next(iter(up.sheets), None)
    if sheet not in up.sheets:
        raise ValidationError(f"Листа «{sheet}» нет. Листы: {', '.join(up.sheets)}")
    data = up.sheets[sheet]
    start_row = max(1, int(start_row))
    rows = max(1, min(int(rows), READ_ROWS))
    part = data[start_row - 1:start_row - 1 + rows]
    return {"file": up.name, "sheet": sheet, "total_rows": len(data), "start_row": start_row,
            "rows": "\n".join(_fmt_row(start_row + i, r) for i, r in enumerate(part))}
