"""适配器协议的不变量：时区强制与不可变数据类。"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse, require_aware
from ragdemo.adapters.errors import AdapterError, RateLimited


def test_require_aware_accepts_tz_aware() -> None:
    value = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    assert require_aware(value, "known_at") is value


def test_require_aware_rejects_naive() -> None:
    with pytest.raises(ValueError, match="known_at"):
        require_aware(datetime(2024, 10, 28, 18, 32), "known_at")


def test_fetch_context_is_frozen() -> None:
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    assert ctx.dry_run is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.ingest_run_id = "r2"  # type: ignore[misc]


def test_raw_response_is_frozen() -> None:
    raw = RawResponse(
        provider="mock",
        endpoint="/x",
        params={"a": 1},
        payload=[],
        http_status=200,
        fetched_at=datetime.now(UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        raw.http_status = 500  # type: ignore[misc]


def test_fact_record_rejects_naive_known_at() -> None:
    with pytest.raises(ValueError, match="known_at"):
        FactRecord(
            entity_ref="688256.SH",
            metric_field="revenue",
            period="2024Q3",
            period_end=date(2024, 9, 30),
            value=1.0,
            unit="CNY",
            currency="CNY",
            valid_from=date(2024, 7, 1),
            known_at=datetime(2024, 10, 28),
            source_ref=None,
        )


def test_fact_record_rejects_known_at_before_period_end() -> None:
    """一份报告不可能在它描述的期间结束前就被知晓——这是
    ragdemo_core.db.invariants.known_at_before_period_end 在 DB 层查的同一条泄漏，
    这里在构造时就拦住，不必等写库再靠 SQL 自检发现。
    """
    with pytest.raises(ValueError, match="known_at"):
        FactRecord(
            entity_ref="688256.SH",
            metric_field="revenue",
            period="2024Q3",
            period_end=date(2024, 9, 30),
            value=1.0,
            unit="CNY",
            currency="CNY",
            valid_from=date(2024, 7, 1),
            known_at=datetime(2024, 9, 1, tzinfo=UTC),
            source_ref=None,
        )


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimited("too many requests", retry_after_s=2.5)
    assert isinstance(err, AdapterError)
    assert err.retry_after_s == 2.5


def test_known_at_must_not_be_in_the_future() -> None:
    """未来的 known_at 意味着「还没发生就知道了」，是最严重的时点缺陷。"""
    future = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(ValueError, match="未来"):
        FactRecord(
            entity_ref="688256.SH",
            metric_field="revenue",
            period="2024Q3",
            period_end=date(2024, 9, 30),
            value=1.0,
            unit="CNY",
            currency="CNY",
            valid_from=date(2024, 7, 1),
            known_at=future,
            source_ref=None,
        )
