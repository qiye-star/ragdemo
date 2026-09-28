"""万得 Wind 财务数据适配器（总计划阶段 2，2026-09-28 选定 Wind 作财务数据源）。

Wind MCP 的 `get_stock_fundamentals` 是自然语言接口：同一个问题的措辞稍有不同，返回的
表结构就会变（逐列宽表 vs 逐期长表）、列名也不照抄问题里的说法、单位按列各不相同。
更要命的是同样的列名下有时是累计值、有时是单季度值。适配器因此用固定问句模板，同时
要累计值与单季度值并逐期核对，对响应做严格校验——宁可整份拒绝，也不把一个可能对错
了期、错了单位、错了口径的数字写进时点库。

夹具都是 2026-09-28 对海光信息（688041.SH）的真实响应，只含公开财务数据：
`fundamentals_long_688041.json` 是现行模板的响应；`fundamentals_ambiguous_688041.json`
是旧模板（"每季的X"）的响应，列名看不出口径而值其实是单季度，必须被拒。
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
    full_years,
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


def _raw(
    payload: Any,  # noqa: ANN401
    *,
    fields: tuple[str, ...] = EIGHT_FIELDS,
    quarter_ends: tuple[date, ...] = (),
    windcode: str = "688041.SH",
) -> RawResponse:
    return RawResponse(
        provider="wind",
        endpoint="get_stock_fundamentals",
        params={
            "windcode": windcode,
            "fields": list(fields),
            "quarter_ends": [d.isoformat() for d in quarter_ends],
            "question": "q",
        },
        payload=payload,
        http_status=200,
        fetched_at=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
    )


def _adapter() -> WindFundamentalsAdapter:
    return WindFundamentalsAdapter(MockMcpGateway(), windcodes=["688041.SH"])


def _records(payload: Any, **kw: Any) -> dict[tuple[str, str], Any]:  # noqa: ANN401
    return {(r.metric_field, r.period): r for r in _adapter().parse(_raw(payload, **kw))}


def _column(payload: Any, name: str) -> int:  # noqa: ANN401
    """列名以 `name` 结尾的那一列（要单季度列就写全 "单季度X"）。"""
    columns = payload["data"]["data"][0]["columns"]
    hits = [
        i
        for i, c in enumerate(columns)
        if c["name"].endswith(name) and ("单季度" in name or "单季度" not in c["name"])
    ]
    assert len(hits) == 1, (name, hits)
    return hits[0]


def _rows(payload: Any) -> list[list[Any]]:  # noqa: ANN401
    rows: list[list[Any]] = payload["data"]["data"][0]["rows"]
    return rows


# --- 解析 ------------------------------------------------------------------------


def test_long_table_yields_every_field_for_every_disclosed_period() -> None:
    records = _records(_payload("fundamentals_long_688041.json"))
    periods = {p for _, p in records}
    assert periods == {"2024Q1", "2024Q2", "2024Q3", "2024FY", "2025Q1"}
    assert {f for f, _ in records} == set(EIGHT_FIELDS)
    assert len(records) == 5 * 8


def test_values_are_cumulative_and_converted_to_yuan_per_column_unit() -> None:
    records = _records(_payload("fundamentals_long_688041.json"))
    revenue = records[("营业收入", "2024Q3")]  # 年初至三季度末累计，不是单季度的 23.7363 亿
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
    drop = _column(payload, "研发费用")
    table["columns"].pop(drop)
    for row in table["rows"]:
        row.pop(drop)
    with pytest.raises(WindResponseRejected, match="研发费用"):
        _records(payload)


def test_column_names_with_underscores_and_duplicate_suffixes_are_normalised() -> None:
    """实测长电科技：'购建固定资产_无形资产和其他长期资产支付的现金'；重名列还会带 `_2`。"""
    payload = _payload("fundamentals_long_688041.json")
    columns = payload["data"]["data"][0]["columns"]
    capex = "购建固定资产、无形资产和其他长期资产支付的现金"
    columns[_column(payload, capex)]["name"] = "每期购建固定资产_无形资产和其他长期资产支付的现金"
    columns[_column(payload, "存货")]["name"] = "每期存货_2"
    records = _records(payload)
    assert (capex, "2024Q2") in records and ("存货", "2024Q2") in records


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
    col = payload["data"]["data"][0]["columns"][_column(payload, "存货")]
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
    _rows(payload)[-1][_column(payload, "实际披露日期")] = None
    assert "2025Q1" not in {p for _, p in _records(payload)}


def test_null_values_are_skipped_not_written_as_zero() -> None:
    payload = _payload("fundamentals_long_688041.json")
    _rows(payload)[0][_column(payload, "存货")] = None
    assert ("存货", "2024Q1") not in _records(payload)


# --- 累计 / 单季度核对 -----------------------------------------------------------


def test_old_template_response_without_single_quarter_columns_is_rejected() -> None:
    """旧模板的真实响应：列名 "每季的营业收入"，值其实是单季度。没有单季度列可核对，拒绝。"""
    with pytest.raises(WindResponseRejected, match="单季度"):
        _records(_payload("fundamentals_ambiguous_688041.json"))


def test_single_quarter_that_does_not_reconcile_rejects_the_whole_response() -> None:
    """把 2024Q3 的"累计"换成单季度值——正是 2026-09-28 实际踩到的混淆——必须整份拒绝。"""
    payload = _payload("fundamentals_long_688041.json")
    _rows(payload)[2][_column(payload, "营业收入")] = 23.7363
    with pytest.raises(WindResponseRejected, match="2024Q3 营业收入"):
        _records(payload)


def test_cumulative_column_is_identified_by_arithmetic_not_by_its_name() -> None:
    """另一次真实响应：没有"单季度"字样，累计叫"各期X"、单季叫"各季度X"，capex 名被截断。"""
    records = _records(_payload("fundamentals_unlabelled_688041.json"))
    assert records[("营业收入", "2024Q3")].value == float(Decimal("61.3654") * Decimal(10**8))
    assert records[("营业收入", "2024FY")].value == float(Decimal("91.6215") * Decimal(10**8))
    capex = records[("购建固定资产、无形资产和其他长期资产支付的现金", "2024Q2")]
    assert capex.value == float(Decimal("4.9925") * Decimal(10**8))


def test_response_split_into_two_long_tables_is_joined_on_period() -> None:
    """真实响应（浪潮信息）：累计一张表、单季度一张表，按 (Wind代码, 日期) 拼起来再核对。"""
    records = _records(_payload("fundamentals_two_tables_000977.json"), windcode="000977.SZ")
    assert records[("营业收入", "2023Q2")].value == float(Decimal("247.976") * Decimal(10**8))
    assert records[("存货", "2023Q1")].value == float(Decimal("175.5058") * Decimal(10**8))
    assert len({p for _, p in records}) == 14


def test_split_tables_with_different_periods_are_rejected() -> None:
    payload = _payload("fundamentals_two_tables_000977.json")
    payload["data"]["data"][1]["rows"].pop()
    with pytest.raises(WindResponseRejected, match="期别不一致"):
        _records(payload, windcode="000977.SZ")


def test_column_labelled_single_quarter_that_adds_up_as_cumulative_is_rejected() -> None:
    payload = _payload("fundamentals_long_688041.json")
    columns = payload["data"]["data"][0]["columns"]
    cum, single = _column(payload, "营业收入"), _column(payload, "单季度营业收入")
    columns[cum]["name"], columns[single]["name"] = "每期单季度营业收入", "每期营业收入"
    with pytest.raises(WindResponseRejected, match="标着单季度"):
        _records(payload)


def test_q1_single_quarter_must_equal_q1_cumulative() -> None:
    payload = _payload("fundamentals_long_688041.json")
    _rows(payload)[0][_column(payload, "单季度研发费用")] = 1.0
    with pytest.raises(WindResponseRejected, match="2024Q1 研发费用"):
        _records(payload)


def test_rounding_within_the_fourth_decimal_is_tolerated() -> None:
    payload = _payload("fundamentals_long_688041.json")
    idx = _column(payload, "单季度营业收入")
    _rows(payload)[2][idx] = round(_rows(payload)[2][idx] + 0.0001, 4)
    assert ("营业收入", "2024Q3") in _records(payload)


def test_flow_item_that_cannot_be_reconciled_is_not_written_and_is_reported() -> None:
    """上一期累计缺失：本期单季度无从核对，这一条不入库（记警告），其他照常。"""
    payload = _payload("fundamentals_long_688041.json")
    _rows(payload)[1][_column(payload, "营业收入")] = None
    result = _adapter().parse_with_warnings(_raw(payload))
    keys = {(r.metric_field, r.period) for r in result.records}
    assert ("营业收入", "2024Q3") not in keys and ("营业收入", "2024Q2") not in keys
    assert ("存货", "2024Q3") in keys
    assert any("2024Q3 营业收入" in w for w in result.warnings)


def test_bad_disclosure_date_skips_that_period_with_a_warning() -> None:
    """实测年报行的披露日期出现过 'Q1 FY2025'：不猜，本期不入库并报告；其余期别不受影响。"""
    payload = _payload("fundamentals_long_688041.json")
    _rows(payload)[3][_column(payload, "实际披露日期")] = "Q1 FY2025"
    result = _adapter().parse_with_warnings(_raw(payload))
    periods = {r.period for r in result.records}
    assert "2024FY" not in periods and "2025Q1" in periods
    assert any("Q1 FY2025" in w for w in result.warnings)


def test_compact_disclosure_date_format_is_accepted() -> None:
    payload = _payload("fundamentals_long_688041.json")
    _rows(payload)[3][_column(payload, "实际披露日期")] = "20250301"
    annual = _records(payload)[("营业收入", "2024FY")]
    assert annual.known_at == datetime.combine(date(2025, 3, 1), time(23, 59, 59), tzinfo=CST)


def test_periods_asked_only_for_reconciliation_are_not_written() -> None:
    records = _records(
        _payload("fundamentals_long_688041.json"),
        quarter_ends=(date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31)),
    )
    assert {p for _, p in records} == {"2024Q3", "2024FY", "2025Q1"}


# --- 取数 ------------------------------------------------------------------------


def test_quarter_ends_back_counts_only_ended_quarters() -> None:
    assert quarter_ends_back(date(2025, 10, 5), 3) == [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
    ]
    assert quarter_ends_back(date(2025, 9, 30), 1) == [date(2025, 9, 30)]
    assert quarter_ends_back(date(2025, 1, 10), 2) == [date(2024, 9, 30), date(2024, 12, 31)]


def test_full_years_extends_back_to_the_first_q1() -> None:
    assert full_years([date(2024, 9, 30), date(2025, 3, 31)]) == [
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
    ]


def test_question_lists_report_dates_and_asks_cumulative_single_quarter_and_disclosure() -> None:
    q = build_question("688041.SH", [date(2024, 9, 30), date(2024, 12, 31)], ("营业收入", "存货"))
    assert q == (
        "688041.SH 报告期20240331、20240630、20240930、20241231的营业收入、单季度营业收入、存货，"
        "以及各报告期定期报告实际披露日期，按报告期逐期列示"
    )


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
