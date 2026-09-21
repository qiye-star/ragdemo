"""Tushare 适配器：known_at 必须取公告日，不是期末日。"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ragdemo.adapters.base import Adapter, RawResponse
from ragdemo.adapters.tushare import TushareAdapter
from tests.contracts.adapter_contract import AdapterContract

CST = timezone(timedelta(hours=8))
FIXTURES = Path("tests/fixtures/tushare")


def _raw(name: str, endpoint: str) -> RawResponse:
    return RawResponse(
        provider="tushare", endpoint=endpoint, params={},
        payload=json.loads((FIXTURES / name).read_text(encoding="utf-8")),
        http_status=200, fetched_at=datetime.now(UTC),
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
        provider="tushare", endpoint="/income", params={},
        payload={"code": 40203, "msg": "积分不足", "data": None},
        http_status=200, fetched_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="40203"):
        list(adapter.parse(bad))
