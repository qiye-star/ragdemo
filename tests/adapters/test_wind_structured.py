"""Wind 前十大股东 / 主营构成解析与校验（阶段二 Task 5/6）。样本是 2026-09-29 的真实录制。"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.structured_rows import SegmentRow
from ragdemo.adapters.wind import WindResponseRejected
from ragdemo.adapters.wind_structured import (
    FUNDAMENTALS_TOOL,
    HOLDERS_TOOL,
    WindHoldersAdapter,
    WindSegmentsAdapter,
    check_against_revenue,
    period_phrase,
)

FIXTURES = Path("tests/fixtures/wind")


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _raw(name: str, windcode: str, period: str, payload: Any = None) -> RawResponse:  # noqa: ANN401
    d = _load(name)
    return RawResponse(
        "wind",
        d["tool"],
        {"windcode": windcode, "period": period},
        d["payload"] if payload is None else payload,
        200,
        datetime.now(UTC),
    )


def _holders(name: str = "holders_688041_2024fy") -> tuple[dict[str, Any], str]:
    return _load(name)["payload"], "688041.SH"


def _idx(table: dict[str, Any], contains: str) -> int:
    return next(i for i, c in enumerate(table["columns"]) if contains in c["name"])


# ---------------------------------------------------------------- 股东


def test_holders_parse_real_sample() -> None:
    result = WindHoldersAdapter(MockMcpGateway()).parse(
        _raw("holders_688041_2024fy", "688041.SH", "2024FY")
    )
    assert result.period == "2024FY" and len(result.rows) == 10
    first = result.rows[0]
    assert (first.rank, first.holder_name) == (1, "曙光信息产业股份有限公司")
    assert first.shares == Decimal("649900000") and first.ratio == Decimal("0.2796")
    assert sum(r.ratio for r in result.rows) <= 1


def test_holders_second_entity_parses() -> None:
    result = WindHoldersAdapter(MockMcpGateway()).parse(
        _raw("holders_000977_2024fy", "000977.SZ", "2024FY")
    )
    assert result.rows[0].ratio == Decimal("0.3193") and len(result.rows) == 10


def test_latest_period_columns_without_a_year_are_rejected() -> None:
    with pytest.raises(WindResponseRejected):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_latest", "688041.SH", "2024FY")
        )


def test_year_only_label_is_not_accepted_for_half_year() -> None:
    """列名只写年份时无法区分年报与中报，只有 FY 允许。"""
    with pytest.raises(WindResponseRejected):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_2024fy", "688041.SH", "2024H1")
        )


def test_wrong_security_in_response_is_rejected() -> None:
    with pytest.raises(WindResponseRejected):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_2024fy", "000977.SZ", "2024FY")
        )


def test_ratio_sum_over_100_percent_is_rejected() -> None:
    payload, code = _holders()
    bad = copy.deepcopy(payload)
    table = bad["data"]["data"][0]
    r = _idx(table, "持股比例")
    for row in table["rows"]:
        row[r] = row[r] * 4
    with pytest.raises(WindResponseRejected):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_2024fy", code, "2024FY", bad)
        )


def test_rank_gap_is_rejected() -> None:
    payload, code = _holders()
    bad = copy.deepcopy(payload)
    table = bad["data"]["data"][0]
    table["rows"][3][_idx(table, "名次")] = 9
    with pytest.raises(WindResponseRejected):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_2024fy", code, "2024FY", bad)
        )


def test_mixed_period_rows_are_caught_by_implied_total_shares() -> None:
    """把一行换成另一期的持股数（同一股东，数量不同）→ 隐含总股本对不上。"""
    payload, code = _holders()
    bad = copy.deepcopy(payload)
    table = bad["data"]["data"][0]
    s = _idx(table, "持股数量")
    table["rows"][4][s] = table["rows"][4][s] * 4 // 5  # 5 号股东股数缩 20%，比例不变，仍递减
    with pytest.raises(WindResponseRejected, match="隐含总股本"):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_2024fy", code, "2024FY", bad)
        )


def test_holders_fetch_asks_for_the_period_and_keeps_the_question() -> None:
    gateway = MockMcpGateway(
        tools=[ToolSpec(HOLDERS_TOOL, "", {})],
        responses={HOLDERS_TOOL: _load("holders_688041_2024fy")["payload"]},
    )
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 29))
    (raw,) = WindHoldersAdapter(gateway).fetch(
        ctx, windcode="688041.SH", name="海光信息", period="2024FY"
    )
    assert "2024年年报(报告期2024-12-31)" in str(raw.params["question"])
    assert raw.params["period"] == "2024FY" and raw.endpoint == HOLDERS_TOOL


def test_period_phrase() -> None:
    assert period_phrase("2025H1") == "2025年中报(报告期2025-06-30)"
    assert period_phrase("2025Q3") == "2025年三季报(报告期2025-09-30)"


# ---------------------------------------------------------------- 分部


def _segments(name: str, code: str, period: str) -> list[SegmentRow]:
    return WindSegmentsAdapter(MockMcpGateway()).parse(_raw(name, code, period))


def test_segments_parse_both_dimensions_and_convert_units_to_yuan() -> None:
    rows = _segments("segments_688041_2024fy", "688041.SH", "2024FY")
    product = {r.name: r for r in rows if r.dimension == "product"}
    # 9134.2991 百万元 = 9,134,299,100 元；毛利 5818.3767 百万元
    assert product["高端处理器"].revenue == Decimal("9134299100")
    assert product["高端处理器"].gross_profit == Decimal("5818376700")
    assert product["高端处理器"].share == Decimal("0.99696")
    assert {r.dimension for r in rows} == {"product", "region"}
    # 空的名次补位行被丢弃
    assert len([r for r in rows if r.dimension == "product"]) == 2


def test_segments_units_can_differ_per_table() -> None:
    """浪潮的收入列是亿元（海光的产品表是百万元）——各按自己的单位换算。"""
    rows = _segments("segments_000977_2024fy", "000977.SZ", "2024FY")
    top = next(r for r in rows if r.name == "服务器及部件")
    assert top.revenue == Decimal("114001610000")  # 1140.0161 亿元


def test_half_year_top5_response_is_accepted_with_its_own_total() -> None:
    rows = _segments("segments_688041_2025h1", "688041.SH", "2025H1")
    assert {r.name for r in rows} == {"高端处理器", "其他业务"}


def test_period_mismatch_between_request_and_column_labels_is_rejected() -> None:
    """列名写的是 2024 年年报，却当成 2023FY 入库——这正是分部数据最危险的错配。"""
    with pytest.raises(WindResponseRejected):
        _segments("segments_688041_2024fy", "688041.SH", "2023FY")
    with pytest.raises(WindResponseRejected):
        _segments("segments_688041_2024fy", "688041.SH", "2024H1")


def test_derived_columns_are_ignored() -> None:
    """浪潮的 `毛利占比` / `毛利计算值` 出现负数、口径混乱：读到它们就会污染毛利。"""
    rows = _segments("segments_000977_2024fy", "000977.SZ", "2024FY")
    top = next(r for r in rows if r.name == "服务器及部件")
    assert top.gross_profit == Decimal("7710550000")  # 取"主营产品毛利"，不取"毛利计算值"


def test_revenue_not_adding_up_to_the_total_is_rejected() -> None:
    d = _load("segments_688041_2024fy")["payload"]
    bad = copy.deepcopy(d)
    table = bad["data"]["data"][0]
    table["rows"][0][_idx(table, "主营产品收入")] *= 1.1
    with pytest.raises(WindResponseRejected):
        WindSegmentsAdapter(MockMcpGateway()).parse(
            _raw("segments_688041_2024fy", "688041.SH", "2024FY", bad)
        )


def test_share_disagreeing_with_revenue_over_total_is_rejected() -> None:
    d = _load("segments_688041_2024fy")["payload"]
    bad = copy.deepcopy(d)
    table = bad["data"]["data"][0]
    table["rows"][1][_idx(table, "主营产品收入占比")] = 20.0  # 技术服务占比应约 0.3%
    with pytest.raises(WindResponseRejected):
        WindSegmentsAdapter(MockMcpGateway()).parse(
            _raw("segments_688041_2024fy", "688041.SH", "2024FY", bad)
        )


def test_quarter_periods_are_not_supported_for_segments() -> None:
    gateway = MockMcpGateway(tools=[ToolSpec(FUNDAMENTALS_TOOL, "", {})], responses={})
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 29))
    with pytest.raises(ValueError):
        list(
            WindSegmentsAdapter(gateway).fetch(ctx, windcode="688041.SH", name="x", period="2025Q1")
        )


def test_check_against_revenue_boundaries() -> None:
    rows = [
        SegmentRow("product", "a", Decimal("600"), None, None),
        SegmentRow("product", "b", Decimal("400"), None, None),
    ]
    check_against_revenue(rows, Decimal("1000"))  # 恰好吻合
    check_against_revenue(rows, Decimal("1004"))  # 0.4% 内
    with pytest.raises(WindResponseRejected):
        check_against_revenue(rows, Decimal("1020"))  # 2% 偏差（错配的期别）
    with pytest.raises(WindResponseRejected):
        check_against_revenue(rows, Decimal("0"))


def test_third_quarter_column_spelled_as_3jimo_is_accepted() -> None:
    """2026-09-29 实测：同一期别的列名写法会变（三季报 / 3季末）。"""
    payload, code = _holders()
    alt = copy.deepcopy(payload)
    for c in alt["data"]["data"][0]["columns"]:
        c["name"] = c["name"].replace("2024年前十大", "2024年3季末的前十大")
    result = WindHoldersAdapter(MockMcpGateway()).parse(
        _raw("holders_688041_2024fy", code, "2024Q3", alt)
    )
    assert len(result.rows) == 10


def test_mismatched_period_word_is_still_rejected() -> None:
    payload, code = _holders()
    alt = copy.deepcopy(payload)
    for c in alt["data"]["data"][0]["columns"]:
        c["name"] = c["name"].replace("2024年前十大", "2024年3季末的前十大")
    with pytest.raises(WindResponseRejected):
        WindHoldersAdapter(MockMcpGateway()).parse(
            _raw("holders_688041_2024fy", code, "2024H1", alt)
        )


def test_column_spelled_without_the_top_ten_words_is_accepted() -> None:
    """同一只股票同一期，Wind 还给过"2024年3季末股东名称"（没有"前十大"三个字）。"""
    payload, code = _holders()
    alt = copy.deepcopy(payload)
    for c in alt["data"]["data"][0]["columns"]:
        c["name"] = c["name"].replace("2024年前十大股东", "2024年3季末股东")
    result = WindHoldersAdapter(MockMcpGateway()).parse(
        _raw("holders_688041_2024fy", code, "2024Q3", alt)
    )
    assert len(result.rows) == 10
