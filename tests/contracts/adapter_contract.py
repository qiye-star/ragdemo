"""任何 Adapter 实现都必须通过的契约。

用法：子类化 AdapterContract，实现 make_adapter()，即自动获得这 5 条测试。
真实适配器与 Mock 走同一套契约——这是 Mock 能替代真实数据推进 P1 的前提。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ragdemo.adapters.base import Adapter, FetchContext


class AdapterContract:
    """契约测试基类。不要在这里加 pytest fixture，子类可能有自己的。"""

    def make_adapter(self) -> Adapter:
        raise NotImplementedError

    def make_context(self, partition_date: date) -> FetchContext:
        return FetchContext(ingest_run_id="contract-run", partition_date=partition_date)

    def test_provider_is_a_nonempty_string(self) -> None:
        assert isinstance(self.make_adapter().provider, str)
        assert self.make_adapter().provider != ""

    def test_health_returns_bool(self) -> None:
        assert isinstance(self.make_adapter().health(), bool)

    def test_known_at_is_timezone_aware_and_not_future(self) -> None:
        adapter = self.make_adapter()
        ctx = self.make_context(date(2024, 10, 28))
        now = datetime.now(UTC)
        for raw in adapter.fetch(ctx):
            for item in _iter_records(raw.payload):
                ka = adapter.known_at(item)
                assert ka.tzinfo is not None and ka.utcoffset() is not None
                assert ka <= now

    def test_fetch_is_idempotent(self) -> None:
        """同一 FetchContext 两次调用必须给出相同结果，否则回填不可复现。"""
        adapter = self.make_adapter()
        ctx = self.make_context(date(2024, 10, 28))
        first = [r.payload for r in adapter.fetch(ctx)]
        second = [r.payload for r in adapter.fetch(ctx)]
        assert first == second

    def test_backfill_known_at_stays_historical(self) -> None:
        """回填两年前的分区时，known_at 必须落在那个历史区间，
        不能是回填当天——否则历史数据在回测中整体消失。
        见 docs/03-point-in-time.md §1.2。
        """
        adapter = self.make_adapter()
        old = date.today() - timedelta(days=730)
        for raw in adapter.fetch(self.make_context(old)):
            for item in _iter_records(raw.payload):
                assert adapter.known_at(item).date() <= old + timedelta(days=90)


def _iter_records(payload: object) -> list[object]:
    return list(payload) if isinstance(payload, list) else [payload]
