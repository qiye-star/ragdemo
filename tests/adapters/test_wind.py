"""万得 Wind 财务数据适配器（总计划阶段 2，2026-09-28 选定 Wind 作财务数据源）。

Wind MCP 的 `get_stock_fundamentals` 是自然语言接口：同一个问题的措辞稍有不同，返回的
表结构就会变（逐列宽表 vs 逐期长表）、列名也不照抄问题里的说法、单位按列各不相同。
适配器因此用固定问句模板，并对响应做严格校验——宁可整份拒绝，也不把一个可能对错
了期、错了单位的数字写进时点库。

夹具是 2026-09-28 对海光信息（688041.SH）的真实响应，只含公开财务数据。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind import (
    WIND_FIELDS,
    WindFundamentalsAdapter,
    WindResponseRejected,
    build_question,
    quarter_ends_back,
)

FIXTURES = Path("tests/fixtures/wind")
CST = timezone(timedelta(hours=8))
EIGHT_FIELDS = (
    "营业收入",
    "归母净利润",
    "研发费用",
    "经营活动产生的现金流量净额",
    "存货",
    "合同负债",
    "预付款项",
    "购建固定资产、无形资产和其他长期资产支付的现金",
)


def _payload(name: str) -> Any:  # noqa: ANN401
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _raw(payload: Any, *, fields: tuple[str, ...] = EIGHT_FIELDS) -> RawResponse:  # noqa: ANN401
    return RawResponse(
        provider="wind",
        endpoint="get_stock_fundamentals",
        params={"windcode": "688041.SH", "fields": list(fields), "question": "q"},
        payload=payload,
        http_status=200,
        fetched_at=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
    )


def _records(payload: Any, **kw: Any) -> dict[tuple[str, str], Any]:  # noqa: ANN401
    adapter = WindFundamentalsAdapter(MockMcpGateway(), windcodes=["688041.SH"])
    return {(r.metric_field, r.period): r for r in adapter.parse(_raw(payload, **kw))}


# --- 解析 ------------------------------------------------------------------------


def test_long_table_yields_every_field_for_every_disclosed_period() -> None:
    records = _records(_payload("fundamentals_long_688041.json"))
    periods = {p for _, p in records}
    assert periods == {"2024Q1", "2024Q2", "2024Q3", "2024FY", "2025Q1"}
    assert {f for f, _ in records} == set(EIGHT_FIELDS)
    assert len(records) == 5 * 8


def test_values_are_converted_to_yuan_per_column_unit() -> None:
    records = _records(_payload("fundamentals_long_688041.json"))
    revenue = records[("营业收入", "2024Q3")]
    assert revenue.value == float(Decimal("61.3654") * Decimal("100000000"))
    assert revenue.unit == "CNY" and revenue.currency == "CNY"
    # 合同负债这一列 Wind 给的是百万元，不是亿元
    liability = records[("合同负债", "2024FY")]
    assert liability.value == float(Decimal("903.4714") * Decimal("1000000"))


def test_known_at_is_the_disclosure_date_end_of_day_beijing() -> None:
    records = _records(_payload("fundamentals_long_688041.json"))
    annual = records[("营业收入", "2024FY")]
    assert annual.known_at == datetime.combine(date(2025, 3, 1), time(23, 59, 59), tzinfo=CST)
    assert annual.period_end == date(2024, 12, 31)
    assert records[("营业收入", "2024Q2")].period_end == date(2024, 6, 30)


def test_flow_items_start_at_year_start_and_balances_at_period_end() -> None:
    """利润表 / 现金流是年初至今累计，资产负债表是期末时点。"""
    records = _records(_payload("fundamentals_long_688041.json"))
    assert records[("营业收入", "2024Q3")].valid_from == date(2024, 1, 1)
    assert records[("存货", "2024Q3")].valid_from == date(2024, 9, 30)


def test_records_are_observed_at_fetch_time_and_keep_a_source_ref() -> None:
    record = _records(_payload("fundamentals_long_688041.json"))[("研发费用", "2025Q1")]
    assert record.observed_at == datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
    assert record.source_ref == "688041.SH:2025Q1:研发费用"
    assert record.entity_ref == "688041.SH"


# --- 拒绝不可信的响应 ------------------------------------------------------------


def test_wide_table_shape_is_rejected() -> None:
    """期别写在列名里的宽表：列名是自然语言，按它推断期别不可靠，整份拒绝。"""
    with pytest.raises(WindResponseRejected, match="日期"):
        _records(_payload("fundamentals_wide_688041.json"), fields=("营业收入",))


def test_missing_requested_field_is_rejected() -> None:
    payload = _payload("fundamentals_long_688041.json")
    table = payload["data"]["data"][0]
    drop = next(i for i, c in enumerate(table["columns"]) if c["name"].endswith("研发费用"))
    table["columns"].pop(drop)
    for row in table["rows"]:
        row.pop(drop)
    with pytest.raises(WindResponseRejected, match="研发费用"):
        _records(payload)


def test_unrecognised_numeric_column_is_rejected() -> None:
    payload = _payload("fundamentals_long_688041.json")
    table = payload["data"]["data"][0]
    table["columns"].append({"name": "某个没问过的指标", "type": "number", "unit": "亿元"})
    for row in table["rows"]:
        row.append(1.0)
    with pytest.raises(WindResponseRejected, match="某个没问过的指标"):
        _records(payload)


def test_unknown_unit_is_rejected() -> None:
    payload = _payload("fundamentals_long_688041.json")
    col = next(c for c in payload["data"]["data"][0]["columns"] if c["name"].endswith("存货"))
    col["unit"] = "千美元"
    with pytest.raises(WindResponseRejected, match="千美元"):
        _records(payload)


def test_unparseable_period_label_is_rejected() -> None:
    payload = _payload("fundamentals_long_688041.json")
    payload["data"]["data"][0]["rows"][0][2] = "最新一期"
    with pytest.raises(WindResponseRejected, match="最新一期"):
        _records(payload)


def test_error_payload_is_rejected() -> None:
    with pytest.raises(WindResponseRejected):
        _records({"data": None, "error": {"message": "额度不足"}})


def test_period_without_disclosure_date_is_skipped() -> None:
    """还没披露的期别不可能"已知"，跳过而不是编一个 known_at。"""
    payload = _payload("fundamentals_long_688041.json")
    table = payload["data"]["data"][0]
    idx = next(i for i, c in enumerate(table["columns"]) if "实际披露日期" in c["name"])
    table["rows"][-1][idx] = None
    assert "2025Q1" not in {p for _, p in _records(payload)}


def test_null_values_are_skipped_not_written_as_zero() -> None:
    payload = _payload("fundamentals_long_688041.json")
    table = payload["data"]["data"][0]
    idx = next(i for i, c in enumerate(table["columns"]) if c["name"].endswith("存货"))
    table["rows"][0][idx] = None
    assert ("存货", "2024Q1") not in _records(payload)


# --- 取数 ------------------------------------------------------------------------


def test_quarter_ends_back_counts_only_ended_quarters() -> None:
    assert quarter_ends_back(date(2025, 10, 5), 3) == [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
    ]
    assert quarter_ends_back(date(2025, 9, 30), 1) == [date(2025, 9, 30)]
    assert quarter_ends_back(date(2025, 1, 10), 2) == [date(2024, 9, 30), date(2024, 12, 31)]


def test_question_asks_for_a_per_quarter_range_and_disclosure_dates() -> None:
    q = build_question("688041.SH", [date(2024, 3, 31), date(2025, 3, 31)], ("营业收入", "存货"))
    assert q.startswith("688041.SH ")
    assert "2024年1季末到2025年1季末每季的营业收入、存货" in q
    assert "每季定期报告实际披露日期" in q


def test_fetch_asks_one_question_per_entity_and_records_the_request() -> None:
    gateway = MockMcpGateway(
        tools=[ToolSpec("get_stock_fundamentals", "", {})],
        responses={"get_stock_fundamentals": _payload("fundamentals_long_688041.json")},
    )
    adapter = WindFundamentalsAdapter(gateway, windcodes=["688041.SH", "000977.SZ"], quarters=5)
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2025, 5, 1))
    raws = list(adapter.fetch(ctx))
    assert [r.params["windcode"] for r in raws] == ["688041.SH", "000977.SZ"]
    assert raws[0].provider == "wind" and raws[0].endpoint == "get_stock_fundamentals"
    assert raws[0].params["fields"] == list(WIND_FIELDS)
    assert [call[0] for call in gateway.calls] == ["get_stock_fundamentals"] * 2


def test_wind_fields_cover_the_standard_statement_metrics() -> None:
    assert set(WIND_FIELDS) == set(EIGHT_FIELDS)
