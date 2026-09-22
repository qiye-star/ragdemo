"""Tushare 适配器：known_at 必须取公告日，不是期末日。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ragdemo.adapters.base import Adapter, FactRecord, FetchContext, RawResponse
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.tushare import TushareAdapter
from tests.contracts.adapter_contract import AdapterContract

CST = timezone(timedelta(hours=8))
FIXTURES = Path("tests/fixtures/tushare")


def _raw(name: str, endpoint: str) -> RawResponse:
    return RawResponse(
        provider="tushare",
        endpoint=endpoint,
        params={},
        payload=json.loads((FIXTURES / name).read_text(encoding="utf-8")),
        http_status=200,
        fetched_at=datetime.now(UTC),
    )


class TestTushareContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return TushareAdapter.for_replay(FIXTURES)


def test_known_at_uses_ann_date_not_end_date() -> None:
    """用 end_date 会让 9 月 30 日就「知道」三季报——典型前视偏差。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    records = list(adapter.parse(_raw("income_2024q3.json", "/income")))
    cam = next(r for r in records if r.entity_ref == "688256.SH")
    assert cam.known_at.astimezone(CST).date() == date(2024, 10, 28)
    assert cam.period_end == date(2024, 9, 30)
    assert cam.known_at.astimezone(CST).date() > cam.period_end


def test_known_at_is_end_of_announcement_day() -> None:
    """只给日期不给时间时取当日 23:59:59——宁可晚知道，不可早知道。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    rec = next(iter(adapter.parse(_raw("income_2024q3.json", "/income"))))
    local = rec.known_at.astimezone(CST)
    assert (local.hour, local.minute, local.second) == (23, 59, 59)


def test_parse_maps_all_metric_fields() -> None:
    adapter = TushareAdapter.for_replay(FIXTURES)
    fields = {r.metric_field for r in adapter.parse(_raw("income_2024q3.json", "/income"))}
    assert fields == {"revenue", "rd_exp"}


def test_price_known_at_is_market_close_not_fetch_time() -> None:
    """行情的 known_at 取 A 股当地收盘 15:30，不取拉取时间。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    rows = list(adapter.parse_prices(_raw("daily_20241028.json", "/daily")))
    assert len(rows) == 1
    local = rows[0].known_at.astimezone(CST)
    assert local.date() == date(2024, 10, 28)
    assert (local.hour, local.minute) == (15, 30)


def test_error_response_raises() -> None:
    adapter = TushareAdapter.for_replay(FIXTURES)
    bad = RawResponse(
        provider="tushare",
        endpoint="/income",
        params={},
        payload={"code": 40203, "msg": "积分不足", "data": None},
        http_status=200,
        fetched_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="40203"):
        list(adapter.parse(bad))


def test_replay_fetch_only_yields_fact_shaped_rows_and_parse_never_crashes() -> None:
    """回放目录里 income_*.json（事实）与 daily_*.json（价格）两种夹具混在一起。

    fetch() 是 FactAdapter 契约方法，Dagster 的 fact_normalized 资产对它的
    每个产出无条件调用 parse()（`for raw in adapter.fetch(ctx): parse(raw)`）。
    价格行没有 end_date，parse() 会 KeyError——所以 fetch() 必须只吐出
    事实形状的夹具，价格夹具（daily_20241028.json）应该被跳过。
    """
    adapter = TushareAdapter.for_replay(FIXTURES)
    ctx = FetchContext(ingest_run_id="test-run", partition_date=date(2024, 10, 28))

    responses = list(adapter.fetch(ctx))
    # 只有 income_2024q3.json 是事实形状；daily_20241028.json 必须被过滤掉。
    assert [r.endpoint for r in responses] == ["/income_2024q3"]

    records: list[FactRecord] = []
    for raw in responses:
        records.extend(adapter.parse(raw))  # 不应抛 KeyError

    assert {r.entity_ref for r in records} == {"688256.SH", "002049.SZ"}
    assert all(isinstance(r, FactRecord) for r in records)


def test_valid_from_is_quarter_start_approximation() -> None:
    """valid_from 取季度起点的近似值（period_end - 89天），与 MockFactAdapter 一致。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    records = list(adapter.parse(_raw("income_2024q3.json", "/income")))
    cam = next(r for r in records if r.entity_ref == "688256.SH")
    assert cam.period_end == date(2024, 9, 30)
    assert cam.valid_from == date(2024, 9, 30) - timedelta(days=89)
    assert cam.valid_from == date(2024, 7, 3)


# --- fetch_daily（阶段 F：经 MCP 网关取行情）---------------------------------


def test_fetch_daily_without_watermark_uses_a_single_trade_date() -> None:
    """没有增量游标时退化为只拉 partition_date 当天——与既有全量拉取行为一致。"""
    gateway = MockMcpGateway(
        responses={"daily": [{"ts_code": "688256.SH", "trade_date": "20241028", "close": 50.0}]}
    )
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))

    responses = list(TushareAdapter().fetch_daily(ctx, gateway, ts_code="688256.SH"))

    assert gateway.calls == [("daily", {"ts_code": "688256.SH", "trade_date": "20241028"})]
    assert len(responses) == 1
    expected = [{"ts_code": "688256.SH", "trade_date": "20241028", "close": 50.0}]
    assert responses[0].payload == expected


def test_fetch_daily_with_a_stale_watermark_pulls_the_gap_since_the_day_after_it() -> None:
    """F2：watermark 是"已确认处理到哪天"，区间从它的次日开始，不是它本身——
    否则 watermark 当天的数据会被重复拉一遍。"""
    gateway = MockMcpGateway(responses={"daily": []})
    ctx = FetchContext(
        ingest_run_id="r1", partition_date=date(2024, 10, 28), watermark="20241025"
    )

    list(TushareAdapter().fetch_daily(ctx, gateway, ts_code="688256.SH"))

    assert gateway.calls == [
        ("daily", {"ts_code": "688256.SH", "start_date": "20241026", "end_date": "20241028"})
    ]


def test_fetch_daily_with_a_watermark_already_covering_this_partition_pulls_nothing() -> None:
    """F2 的核心场景：同一分区重跑，watermark 已经等于 partition_date——
    区间为空，不必也不应该再调一次网关，拉取的记录数应显著少于第一次
    （这里是 0）。"""
    gateway = MockMcpGateway(responses={"daily": [{"ts_code": "688256.SH", "close": 1.0}]})
    ctx = FetchContext(
        ingest_run_id="r1", partition_date=date(2024, 10, 28), watermark="20241028"
    )

    responses = list(TushareAdapter().fetch_daily(ctx, gateway, ts_code="688256.SH"))

    assert responses == []
    assert gateway.calls == []


def test_fetch_daily_result_feeds_parse_prices_directly() -> None:
    """网关的 daily 工具直接返回展开过的行字典列表（真实网关实测确认，不是
    Tushare REST 那种 fields/items 列式结构）——_rows() 原样透传，parse_prices()
    不需要额外适配。"""
    gateway = MockMcpGateway(
        responses={
            "daily": [
                {
                    "ts_code": "688256.SH",
                    "trade_date": "20241028",
                    "open": 49.0,
                    "high": 51.0,
                    "low": 48.5,
                    "close": 50.0,
                    "pre_close": 49.5,
                    "vol": 1000.0,
                    "amount": 50000.0,
                }
            ]
        }
    )
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    adapter = TushareAdapter()

    (raw,) = list(adapter.fetch_daily(ctx, gateway, ts_code="688256.SH"))
    (row,) = list(adapter.parse_prices(raw))

    assert row.entity_ref == "688256.SH"
    assert row.close == 50.0
    assert row.adj_factor == 1.0  # daily 工具不带这个字段，parse_prices 兜底
