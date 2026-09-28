"""上市日期取数：A 股一次 Tushare `stock_basic`，其余逐只问 Wind；每份响应都留快照。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.seed.listing_dates import CurationRow, fetch_listing_dates
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, Path("db/migrations"))
    c.commit()
    yield c
    c.close()


def _rest(body: Any, seen: list[dict[str, Any]] | None = None) -> HttpClient:  # noqa: ANN401
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    return HttpClient(
        "tushare-rest",
        "http://proxy.test",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


STOCK_BASIC = {
    "code": 0,
    "msg": "",
    "data": {
        "fields": ["ts_code", "name", "list_date"],
        "items": [["688041.SH", "海光信息", "20220812"], ["000977.SZ", "浪潮信息", "20000608"]],
    },
}


def _wind_answer(code: str, day: str) -> dict[str, Any]:
    return {
        "data": {
            "data": [
                {
                    "columns": [
                        {"name": "Wind代码", "type": "string"},
                        {"name": "证券简称", "type": "string"},
                        {"name": "首发上市日期", "type": "date"},
                    ],
                    "rows": [[code, "x", day]],
                }
            ]
        },
        "error": None,
    }


class _Wind(MockMcpGateway):
    """按问句里的代码作答。"""

    def __init__(self, answers: dict[str, dict[str, Any]], tool: str) -> None:
        super().__init__(tools=[ToolSpec(tool, "", {})])
        self._answers = answers
        self._tool = tool

    def call_tool(self, name: str, arguments: Any) -> Any:  # noqa: ANN401
        code = str(arguments["question"]).split("首发")[0]
        self._responses[name] = self._answers[code]
        return super().call_tool(name, arguments)


ROWS = [
    CurationRow("CN.688041", "listed", "688041.SH", "688041.SH"),
    CurationRow("CN.000977", "listed", "000977.SZ", "000977.SZ"),
    CurationRow("US.NVDA", "listed", "", "NVDA.O"),
    CurationRow("CN.SMEE", "private", "", ""),
]


@pytest.mark.db
def test_a_shares_come_from_one_stock_basic_call_and_the_rest_from_wind(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    seen: list[dict[str, Any]] = []
    wind = _Wind({"NVDA.O": _wind_answer("NVDA.O", "1999-01-22")}, "get_global_stock_basicinfo")
    result = fetch_listing_dates(
        ROWS,
        rest=_rest(STOCK_BASIC, seen),
        wind_global=wind,
        wind_stock=None,
        conn=conn,
        blob=LocalBlobStore(tmp_path),
        ingest_run_id="t",
        today=date(2026, 9, 28),
    )
    assert len(seen) == 1 and seen[0]["api_name"] == "stock_basic"
    got = {d.entity_id: (d.listed_date, d.source) for d in result.dates}
    assert got == {
        "CN.688041": (date(2022, 8, 12), "tushare:stock_basic"),
        "CN.000977": (date(2000, 6, 8), "tushare:stock_basic"),
        "US.NVDA": (date(1999, 1, 22), "wind:get_global_stock_basicinfo"),
    }
    assert result.failures == {}, "非上市公司不取上市日期，也不算失败"
    snaps = conn.execute("SELECT count(*) FROM core.provider_snapshot").fetchone()
    assert snaps is not None and snaps[0] == 2


@pytest.mark.db
def test_a_share_missing_from_stock_basic_falls_back_to_wind(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    refused = {
        "code": 40203,
        "msg": "抱歉，您访问接口(stock_basic)频率超限(1次/分钟)",
        "data": None,
    }
    wind_stock = _Wind(
        {
            "688041.SH": _wind_answer("688041.SH", "2022-08-12"),
            "000977.SZ": _wind_answer("000977.SZ", "2000-06-08"),
        },
        "get_stock_basicinfo",
    )
    result = fetch_listing_dates(
        ROWS[:2],
        rest=_rest(refused),
        wind_global=_Wind({}, "get_global_stock_basicinfo"),
        wind_stock=wind_stock,
        conn=conn,
        blob=LocalBlobStore(tmp_path),
        ingest_run_id="t",
        today=date(2026, 9, 28),
    )
    assert {d.source for d in result.dates} == {"wind:get_stock_basicinfo"}
    assert len(result.dates) == 2


@pytest.mark.db
def test_wind_answer_for_another_code_is_a_failure_not_a_date(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    wind = _Wind({"NVDA.O": _wind_answer("NVDL.O", "2022-12-13")}, "get_global_stock_basicinfo")
    result = fetch_listing_dates(
        [ROWS[2]],
        rest=_rest(STOCK_BASIC),
        wind_global=wind,
        wind_stock=None,
        conn=conn,
        blob=LocalBlobStore(tmp_path),
        ingest_run_id="t",
        today=date(2026, 9, 28),
    )
    assert result.dates == [] and "NVDL.O" in result.failures["US.NVDA"]
