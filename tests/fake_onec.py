"""Имитация стандартного интерфейса OData 1С для тестов (httpx.MockTransport)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

BASE = "http://1c.local/fler/odata/standard.odata"
USER, PASSWORD = "bot", "secret"
ORG_FLER = "11111111-1111-1111-1111-111111111111"
ORG_OTHER = "22222222-2222-2222-2222-222222222222"

ORGS = [{"Ref_Key": ORG_FLER, "Description": "ООО «Флер»"}, {"Ref_Key": ORG_OTHER, "Description": "ООО «Другая»"}]
# движения регистра продаж: (дата, организация, сумма без НДС)
SALES = [("2026-10-05", ORG_FLER, 1_000_000), ("2026-10-20", ORG_FLER, 500_000), ("2026-10-21", ORG_OTHER, 9_000_000),
         ("2026-11-02", ORG_FLER, 700_000), ("2026-11-15", ORG_FLER, 300_000)]
ORDERS = [("2026-10-03T10:00:00", ORG_FLER, True), ("2026-10-09T12:00:00", ORG_FLER, True),
          ("2026-10-10T12:00:00", ORG_FLER, False), ("2026-11-01T09:00:00", ORG_FLER, True)]
CASH = [("2026-09-30", ORG_FLER, 2_000_000), ("2026-10-15", ORG_FLER, -500_000), ("2026-11-10", ORG_FLER, 100_000)]

METADATA = """<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
<edmx:DataServices><Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">
<EntityType Name="Catalog_Организации"><Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Description" Type="Edm.String"/></EntityType>
<EntityType Name="Document_ЗаказКлиента"><Property Name="Ref_Key" Type="Edm.Guid"/><Property Name="Date" Type="Edm.DateTime"/>
<Property Name="Posted" Type="Edm.Boolean"/><Property Name="Организация_Key" Type="Edm.Guid"/><NavigationProperty Name="Организация"/></EntityType>
<EntityType Name="AccumulationRegister_ВыручкаИСебестоимостьПродаж"><Property Name="Period" Type="Edm.DateTime"/>
<Property Name="Организация_Key" Type="Edm.Guid"/><Property Name="СуммаВыручкиБезНДС" Type="Edm.Double"/></EntityType>
<EntityType Name="AccumulationRegister_ВыручкаИСебестоимостьПродаж_Turnover"><Property Name="Организация_Key" Type="Edm.Guid"/>
<Property Name="СуммаВыручкиБезНДСTurnover" Type="Edm.Double"/></EntityType>
<EntityType Name="AccumulationRegister_ДенежныеСредства_Balance"><Property Name="Организация_Key" Type="Edm.Guid"/>
<Property Name="СуммаBalance" Type="Edm.Double"/></EntityType>
<EntityType Name="AccumulationRegister_ДенежныеСредства_Turnover"><Property Name="СуммаTurnover" Type="Edm.Double"/></EntityType>
</Schema></edmx:DataServices></edmx:Edmx>"""

SETS = ["Catalog_Организации", "Catalog_Номенклатура", "Document_ЗаказКлиента", "Document_РеализацияТоваровУслуг",
        "AccumulationRegister_ВыручкаИСебестоимостьПродаж", "AccumulationRegister_ДенежныеСредства"]

_DT = r"datetime'([\d\-T:]+)'"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _match(row: dict, flt: str | None) -> bool:
    """Понимает только то, что нужно тестам: «поле eq guid'…'», «поле eq true/false», ge/le datetime, and."""
    if not flt:
        return True
    for part in re.split(r"\s+and\s+", flt.replace("(", "").replace(")", "")):
        m = re.match(r"(\w+) (eq|ge|le) (.+)", part.strip())
        assert m, f"фильтр не понят: {part}"
        f, op, val = m.groups()
        cur = row.get(f)
        if val.startswith("guid'"):
            ok = cur == val[5:-1]
        elif val in ("true", "false"):
            ok = cur == (val == "true")
        elif val.startswith("datetime'"):
            d = _dt(val[9:-1])
            ok = _dt(cur) >= d if op == "ge" else _dt(cur) <= d if op == "le" else _dt(cur) == d
        else:
            raise AssertionError(val)
        if not ok:
            return False
    return True


class FakeOneC:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.method == "GET", "в 1С только читаем"
        if req.headers.get("authorization") != httpx.BasicAuth(USER, PASSWORD)._auth_header:
            return httpx.Response(401, text="Unauthorized")
        raw = str(req.url)
        assert raw.startswith(BASE), raw
        parts = urlsplit(raw)
        path = unquote(parts.path)[len(urlsplit(BASE).path):].lstrip("/")
        q = dict(parse_qsl(parts.query))
        if path == "":
            return httpx.Response(200, json={"value": [{"name": s, "url": s} for s in SETS]})
        if path == "$metadata":
            return httpx.Response(200, text=METADATA, headers={"content-type": "application/xml"})
        count = path.endswith("/$count")
        path = path.removesuffix("/$count")
        rows = self.rows(path)
        if rows is None:
            return httpx.Response(404, json={"odata.error": {"code": "6", "message": {
                "lang": "ru", "value": f"Не найден сегмент {path}"}}})
        rows = [r for r in rows if _match(r, q.get("$filter"))]
        if count:
            return httpx.Response(200, text=str(len(rows)))
        skip, top = int(q.get("$skip", 0)), int(q.get("$top", 10**9))
        rows = rows[skip:skip + top]
        if sel := q.get("$select"):
            fields = sel.split(",")
            if rows and (bad := [f for f in fields if f not in rows[0]]):
                return httpx.Response(400, json={"odata.error": {"code": "9", "message": {
                    "lang": "ru", "value": f"Неправильное имя поля: {bad[0]}"}}})
            rows = [{k: r.get(k) for k in fields} for r in rows]
        return httpx.Response(200, content=json.dumps({"odata.metadata": "…", "value": rows}, ensure_ascii=False),
                              headers={"content-type": "application/json"})

    def rows(self, path: str) -> list[dict] | None:
        if path == "Catalog_Организации":
            return ORGS
        if path == "Document_ЗаказКлиента":
            return [{"Ref_Key": f"o{i}", "Date": d, "Организация_Key": o, "Posted": p}
                    for i, (d, o, p) in enumerate(ORDERS)]
        if m := re.fullmatch(rf"AccumulationRegister_ВыручкаИСебестоимостьПродаж/Turnovers\(StartPeriod={_DT},"
                             rf"EndPeriod={_DT}\)", path):
            a, b = _dt(m.group(1)), _dt(m.group(2))
            by_org: dict[str, float] = {}
            for d, o, v in SALES:
                if a <= _dt(d) <= b:
                    by_org[o] = by_org.get(o, 0) + v
            return [{"Организация_Key": o, "СуммаВыручкиБезНДСTurnover": v} for o, v in by_org.items()]
        if m := re.fullmatch(rf"AccumulationRegister_ДенежныеСредства/Balance\(Period={_DT}\)", path):
            p = _dt(m.group(1))
            total = sum(v for d, o, v in CASH if _dt(d) < p)
            return [{"Организация_Key": ORG_FLER, "СуммаBalance": total}]
        return None
