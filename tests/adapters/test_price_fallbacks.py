"""行情的第 2、3 层（ADR-0012）：Tushare REST 与 Wind 日 K 线。

夹具都是 2026-09-28 的真实响应（688041.SH，公开行情）。关键事实：
- Wind `get_stock_kline` 默认前复权（2025-05-06 返回 149.71，不复权收盘是 150.01），
  前复权价随以后的分红变，是前视数据。逐日取、`afdate` 设成当天，得到的是不复权价。
- Wind `VOLUME`（股）÷ 100 = Tushare `vol`（手）；`TURNOVER`（元）÷ 1000 = `amount`（千元），
  两者都能精确换算——这是 Wind 能当行情降级层的前提。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.errors import ProviderRefused
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.tushare import TushareAdapter
from ragdemo.adapters.wind_kline import WindKlineAdapter, WindKlineRejected

WIND = Path("tests/fixtures/wind")
TUSHARE = Path("tests/fixtures/tushare_rest")
CST = timezone(timedelta(hours=8))
DAY = date(2025, 5, 7)


def _json(path: Path) -> Any:  # noqa: ANN401
    return json.loads(path.read_text(encoding="utf-8"))


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


# --- Tushare REST ----------------------------------------------------------------


def test_rest_daily_asks_the_same_question_as_the_mcp_path() -> None:
    seen: list[dict[str, Any]] = []
    client = _rest(_json(TUSHARE / "rest_daily_688041_20250507.json"), seen)
    ctx = FetchContext(ingest_run_id="t", partition_date=DAY)
    raws = list(TushareAdapter().fetch_daily_rest(ctx, client, ts_code="688041.SH"))
    assert seen == [
        {
            "api_name": "daily",
            "params": {"ts_code": "688041.SH", "trade_date": "20250507"},
            "fields": "",
        }
    ]
    (row,) = [r for raw in raws for r in TushareAdapter().parse_prices(raw)]
    assert (row.close, row.volume, row.amount, row.pre_close) == (
        150.8,
        126742.45,
        1910156.347,
        150.01,
    )
    assert raws[0].endpoint == "daily"


def test_rest_daily_respects_the_watermark_like_the_mcp_path() -> None:
    client = _rest(_json(TUSHARE / "rest_daily_688041_20250507.json"))
    ctx = FetchContext(ingest_run_id="t", partition_date=DAY, watermark="20250507")
    assert list(TushareAdapter().fetch_daily_rest(ctx, client, ts_code="688041.SH")) == []


@pytest.mark.parametrize(
    ("fixture", "text"),
    [
        ("rest_refused_no_permission.json", "访问权限"),
        ("rest_refused_rate_limited.json", "1次/分钟"),
    ],
)
def test_rest_refusal_raises_provider_refused_with_the_provider_message(
    fixture: str, text: str
) -> None:
    client = _rest(_json(TUSHARE / fixture))
    ctx = FetchContext(ingest_run_id="t", partition_date=DAY)
    with pytest.raises(ProviderRefused, match=text):
        list(TushareAdapter().fetch_daily_rest(ctx, client, ts_code="688041.SH"))


# --- Wind 日 K 线 ------------------------------------------------------------------


def _wind(payload: Any) -> tuple[WindKlineAdapter, MockMcpGateway]:  # noqa: ANN401
    gateway = MockMcpGateway(
        tools=[ToolSpec("get_stock_kline", "", {})], responses={"get_stock_kline": payload}
    )
    return WindKlineAdapter(gateway), gateway


def _day_rows(payload: Any, day: date = DAY) -> list[Any]:  # noqa: ANN401
    adapter, _ = _wind(payload)
    raw = adapter.fetch_day(FetchContext(ingest_run_id="t", partition_date=day), "688041.SH", day)
    return list(adapter.parse_prices(raw))


def test_wind_asks_for_one_unadjusted_day() -> None:
    adapter, gateway = _wind(_json(WIND / "kline_day_688041_20250507.json"))
    adapter.fetch_day(FetchContext(ingest_run_id="t", partition_date=DAY), "688041.SH", DAY)
    assert gateway.calls == [
        (
            "get_stock_kline",
            {
                "windcode": "688041.SH",
                "begin_date": "20250507",
                "end_date": "20250507",
                "afdate": "20250507",
                "period": "10",
                "issusp": "1",
            },
        )
    ]


def test_wind_day_converts_exactly_to_tushare_units() -> None:
    (row,) = _day_rows(_json(WIND / "kline_day_688041_20250507.json"))
    assert (row.open, row.high, row.low, row.close) == (153.41, 153.41, 149.4, 150.8)
    assert row.volume == 126742.45 and row.amount == 1910156.347  # 与 Tushare 同日逐位相同
    assert row.pre_close is None, "单日请求拿不到昨收，不编"
    assert row.source == "wind" and row.entity_ref == "688041.SH"
    assert row.known_at == datetime.combine(DAY, time(15, 30), tzinfo=CST)
    assert not row.is_suspended


def test_wind_non_trading_day_yields_nothing() -> None:
    assert _day_rows(_json(WIND / "kline_holiday_688041_20250503.json"), date(2025, 5, 3)) == []


def test_wind_row_for_another_day_is_rejected() -> None:
    with pytest.raises(WindKlineRejected, match="2025-05-07"):
        _day_rows(_json(WIND / "kline_day_688041_20250507.json"), date(2025, 5, 8))


def test_wind_unexpected_unit_is_rejected() -> None:
    payload = _json(WIND / "kline_day_688041_20250507.json")
    payload["data"]["unit"]["VOLUME 单位："] = "手"
    with pytest.raises(WindKlineRejected, match="VOLUME"):
        _day_rows(payload)


def test_wind_missing_column_is_rejected() -> None:
    payload = _json(WIND / "kline_day_688041_20250507.json")
    idx = [c["name"] for c in payload["data"]["columns"]].index("TURNOVER")
    payload["data"]["columns"].pop(idx)
    for row in payload["data"]["rows"]:
        row.pop(idx)
    with pytest.raises(WindKlineRejected, match="TURNOVER"):
        _day_rows(payload)


def test_wind_error_payload_is_rejected() -> None:
    with pytest.raises(WindKlineRejected):
        _day_rows({"data": None, "error": {"message": "服务暂不可用"}})
