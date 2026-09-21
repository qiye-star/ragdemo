"""MockFactAdapter 必须通过全部适配器契约，并支持注入自定义记录。"""

from __future__ import annotations

from datetime import date

from ragdemo.adapters.base import Adapter, FetchContext
from ragdemo.adapters.mock.facts import MockFactAdapter
from tests.contracts.adapter_contract import AdapterContract


class TestMockFactAdapterContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return MockFactAdapter()


def test_parse_yields_fact_records() -> None:
    adapter = MockFactAdapter()
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    records = [rec for raw in adapter.fetch(ctx) for rec in adapter.parse(raw)]
    assert records
    assert all(r.known_at.tzinfo is not None for r in records)


def test_call_count_tracks_fetches() -> None:
    adapter = MockFactAdapter()
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    list(adapter.fetch(ctx))
    list(adapter.fetch(ctx))
    assert adapter.call_count == 2
