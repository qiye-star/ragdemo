"""种子实体的上市日期，从供应商取，不手填（`listed_date` 用于剔除次新股，填错会悄悄影响评分）。

- A 股：Tushare REST `stock_basic` 一次拉全市场（该接口限频 1 次/分钟，逐只问会被拒）；
  不在结果里或被拒的，退到 Wind `stock_data.get_stock_basicinfo` 逐只问。
- 港美台日韩：Wind `global_stock_data.get_global_stock_basicinfo` 逐只问"首发上市日期"。
- 非上市公司不取。

每份响应都记 provider_snapshot（CLAUDE.md §1.5）。结果写进
`db/seed/sources/listing_dates.csv`，每行带来源与取数日期，种子构建离线读它。
Wind 答的是另一只证券（自然语言接口会"纠错"到相近代码），记为失败，不采用。
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg

from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.http import HttpClient
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo_core.blob import BlobStore

LISTING_DATES_CSV = Path("db/seed/sources/listing_dates.csv")
FIELDS = ("entity_id", "listed_date", "source", "fetched_on")


@dataclass(frozen=True)
class CurationRow:
    entity_id: str
    entity_type: str
    tushare_code: str
    wind_code: str


@dataclass(frozen=True)
class ListingDate:
    entity_id: str
    listed_date: date
    source: str
    fetched_on: date


@dataclass
class ListingResult:
    dates: list[ListingDate] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)  # entity_id → 原因
    notes: list[str] = field(default_factory=list)  # 被降级的来源等，供人看


def fetch_listing_dates(
    rows: Sequence[CurationRow],
    *,
    rest: HttpClient,
    wind_global: GatewayClient,
    wind_stock: GatewayClient | None,
    conn: psycopg.Connection,
    blob: BlobStore,
    ingest_run_id: str,
    today: date,
) -> ListingResult:
    result = ListingResult()
    listed = [r for r in rows if r.entity_type == "listed"]
    a_shares = [r for r in listed if r.tushare_code]

    tushare: dict[str, date] = {}
    if a_shares:
        try:
            raw = rest.post_json(
                "/",
                {
                    "api_name": "stock_basic",
                    "params": {"list_status": "L"},
                    "fields": "ts_code,name,list_date",
                },
            )
            record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
            conn.commit()
            tushare = _stock_basic_dates(raw.payload)
        except AdapterError as exc:
            result.notes.append(f"tushare stock_basic 不可用，A 股改问 Wind：{str(exc)[:300]}")

    for row in listed:
        if row.tushare_code and row.tushare_code in tushare:
            result.dates.append(
                ListingDate(row.entity_id, tushare[row.tushare_code], "tushare:stock_basic", today)
            )
            continue
        gateway, tool = (
            (wind_stock, "get_stock_basicinfo")
            if row.tushare_code
            else (wind_global, "get_global_stock_basicinfo")
        )
        if gateway is None or not row.wind_code:
            result.failures[row.entity_id] = "没有可用的上市日期来源"
            continue
        try:
            call = gateway.call_tool(tool, {"question": f"{row.wind_code}首发上市日期"})
            record_snapshot(conn, call.raw, ingest_run_id=ingest_run_id, blob=blob)
            conn.commit()
            listed_on = _wind_date(call.payload, row.wind_code)
        except (AdapterError, ValueError) as exc:
            result.failures[row.entity_id] = str(exc)[:300]
            continue
        result.dates.append(ListingDate(row.entity_id, listed_on, f"wind:{tool}", today))
    return result


def _stock_basic_dates(payload: Any) -> dict[str, date]:  # noqa: ANN401
    if not isinstance(payload, dict) or payload.get("code") != 0:
        msg = payload.get("msg") if isinstance(payload, dict) else payload
        raise AdapterError(f"tushare stock_basic 被拒：{msg}")
    data = payload["data"]
    fields = list(data["fields"])
    out: dict[str, date] = {}
    for item in data["items"]:
        row = dict(zip(fields, item, strict=True))
        out[str(row["ts_code"])] = datetime.strptime(str(row["list_date"]), "%Y%m%d").date()
    return out


def _wind_date(payload: Any, wind_code: str) -> date:  # noqa: ANN401
    tables = ((payload or {}).get("data") or {}).get("data") or []
    if len(tables) != 1:
        raise ValueError(f"Wind 应返回一张表，实际 {len(tables)} 张")
    table = tables[0]
    names = [str(c.get("name", "")) for c in table["columns"]]
    idx = [i for i, n in enumerate(names) if "上市日期" in n]
    if len(idx) != 1 or "Wind代码" not in names:
        raise ValueError(f"Wind 响应缺少上市日期列：{names}")
    rows = table["rows"]
    if len(rows) != 1:
        raise ValueError(f"Wind 应返回一行，实际 {len(rows)} 行")
    got = str(rows[0][names.index("Wind代码")])
    if got != wind_code:
        raise ValueError(f"问的是 {wind_code}，Wind 答的是 {got}")
    return date.fromisoformat(str(rows[0][idx[0]])[:10])


def write_listing_dates(dates: Sequence[ListingDate], path: Path = LISTING_DATES_CSV) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        for d in sorted(dates, key=lambda x: x.entity_id):
            writer.writerow(
                [d.entity_id, d.listed_date.isoformat(), d.source, d.fetched_on.isoformat()]
            )


def read_listing_dates(path: Path = LISTING_DATES_CSV) -> dict[str, ListingDate]:
    with path.open(encoding="utf-8", newline="") as f:
        return {
            r["entity_id"]: ListingDate(
                r["entity_id"],
                date.fromisoformat(r["listed_date"]),
                r["source"],
                date.fromisoformat(r["fetched_on"]),
            )
            for r in csv.DictReader(f)
        }


def read_curation(path: Path) -> list[CurationRow]:
    with path.open(encoding="utf-8", newline="") as f:
        return [
            CurationRow(r["entity_id"], r["entity_type"], r["tushare_code"], r["wind_code"])
            for r in csv.DictReader(f)
        ]
