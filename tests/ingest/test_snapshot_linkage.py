"""原始响应留存与回放（docs/04-ingestion.md §2.1、CLAUDE.md §1.5）。

每一次真实取数都要先把原始响应写进 `core.provider_snapshot`，再解析；解析出的
每一行事实 / 行情都带 `snapshot_id` 指回那一次响应。验收：任取一行，能从它的
快照重新解析出同一行。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from dagster import build_asset_context

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse
from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.tushare import PriceRow, TushareAdapter
from ragdemo.ingest.assets import (
    fact_normalized,
    fin_fact_loaded,
    price_daily_loaded,
    price_normalized,
)
from ragdemo.ingest.snapshot import MAX_INLINE_PAYLOAD_BYTES
from ragdemo.ingest.writer import PointInTimeWriter
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
PARTITION = "2024-10-28"


@pytest.fixture
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES "
        "('CN.688256','寒武纪-U','listed','算力','AI芯片',ARRAY['云端训练芯片'],"
        " '云端训练芯片','688256.SH'),"
        "('CN.002049','紫光国微','listed','算力','AI芯片',ARRAY['云端训练芯片'],"
        " '云端训练芯片','002049.SZ')"
    )
    for metric in ("revenue_total", "rd_expense"):
        c.execute(
            "INSERT INTO core.node_metric (metric_id, metric_name, metric_role,"
            " frequency, source_type, definition, unit) "
            "VALUES (%s,%s,'confirming','quarterly','filing','口径','CNY')",
            (metric, metric),
        )
        c.execute(
            "INSERT INTO core.metric_source_map (metric_id, provider, provider_field) "
            "VALUES (%s,'mock',%s)",
            (metric, metric),
        )
    c.commit()
    return c


@pytest.fixture
def blob(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path)


def _snapshot_as_raw(
    conn: psycopg.Connection, snapshot_id: int, blob: LocalBlobStore
) -> RawResponse:
    row = conn.execute(
        "SELECT provider, endpoint, params, response, response_ref, http_status, fetched_at"
        "  FROM core.provider_snapshot WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()
    assert row is not None, f"snapshot {snapshot_id} 不存在"
    provider, endpoint, params, response, response_ref, status, fetched_at = row
    if response is None:
        assert response_ref is not None, "既无内联响应也无 response_ref——原始响应丢了"
        response = json.loads(blob.get(cast(str, response_ref)))
    return RawResponse(
        provider=cast(str, provider),
        endpoint=cast(str, endpoint),
        params=cast(dict[str, Any], params),
        payload=response,
        http_status=cast(int, status),
        fetched_at=cast(datetime, fetched_at),
    )


def _count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return cast(int, row[0])


# --- 财务事实 --------------------------------------------------------------------


@pytest.mark.db
def test_fact_fetch_writes_a_snapshot_before_parsing(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    before = _count(conn, "SELECT count(*) FROM core.provider_snapshot")
    ctx = build_asset_context(partition_key=PARTITION)
    records = cast(list[FactRecord], fact_normalized(ctx, MockFactAdapter(), conn, blob))

    assert _count(conn, "SELECT count(*) FROM core.provider_snapshot") == before + 1
    assert records and all(r.snapshot_id is not None for r in records)


@pytest.mark.db
def test_every_loaded_fact_points_at_a_replayable_snapshot(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    ctx = build_asset_context(partition_key=PARTITION)
    adapter = MockFactAdapter()
    records = cast(list[FactRecord], fact_normalized(ctx, adapter, conn, blob))
    fin_fact_loaded(ctx, records, PointInTimeWriter(conn, ingest_run_id="r1", source="mock"))

    rows = conn.execute(
        "SELECT e.tushare_code, f.metric_id, f.period, f.value, f.snapshot_id"
        "  FROM core.fin_fact f JOIN core.entity e USING (entity_id)"
    ).fetchall()
    assert rows, "前置条件：应该写入了事实"
    assert all(r[4] is not None for r in rows), "每一行事实都必须带 snapshot_id"

    for ts_code, metric_id, period, value, snapshot_id in rows:
        replayed = adapter.parse(_snapshot_as_raw(conn, cast(int, snapshot_id), blob))
        assert any(
            r.entity_ref == ts_code
            and r.metric_field == metric_id
            and r.period == period
            and abs(r.value - float(cast(float, value))) < 1e-9
            for r in replayed
        ), f"快照 {snapshot_id} 回放不出 {ts_code}/{metric_id}/{period}"


@pytest.mark.db
def test_snapshot_survives_a_failed_load(conn: psycopg.Connection, blob: LocalBlobStore) -> None:
    """快照在解析之前就提交：下游写库失败，原始响应也不能跟着丢。"""
    ctx = build_asset_context(partition_key=PARTITION)
    fact_normalized(ctx, MockFactAdapter(), conn, blob)
    conn.rollback()  # 模拟下游资产在同一连接上失败回滚
    assert _count(conn, "SELECT count(*) FROM core.provider_snapshot") == 1


# --- 行情 ------------------------------------------------------------------------


@pytest.mark.db
def test_every_loaded_price_points_at_a_replayable_snapshot(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    gateway = MockMcpGateway(
        responses={
            "daily": [
                {"ts_code": "688256.SH", "trade_date": "20241028", "close": 50.0},
            ]
        }
    )
    ctx = build_asset_context(partition_key=PARTITION)
    rows = cast(list[PriceRow], price_normalized(ctx, gateway, conn, blob))
    assert rows and all(r.snapshot_id is not None for r in rows)

    price_daily_loaded(
        ctx, rows, PointInTimeWriter(conn, ingest_run_id="r1", source="tushare"), conn
    )
    loaded = conn.execute(
        "SELECT e.tushare_code, p.trade_date, p.close, p.snapshot_id"
        "  FROM core.price_daily p JOIN core.entity e USING (entity_id)"
    ).fetchall()
    assert loaded and all(r[3] is not None for r in loaded)

    adapter = TushareAdapter()
    for ts_code, trade_date, close, snapshot_id in loaded:
        replayed = adapter.parse_prices(_snapshot_as_raw(conn, cast(int, snapshot_id), blob))
        assert any(
            r.entity_ref == ts_code and r.trade_date == trade_date and r.close == float(close)
            for r in replayed
        )


class _AdapterLeakingToken(MockFactAdapter):
    """模拟一个把 token 放进调用参数的供应商（有些供应商真这么做）。"""

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        return super().fetch(ctx, token="should-not-be-stored", **params)


@pytest.mark.db
def test_snapshot_params_never_contain_credentials(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    """凭据只在出网代理层（CLAUDE.md §3）；万一调用参数里混进了 token，也不得落库。"""
    ctx = build_asset_context(partition_key=PARTITION)
    fact_normalized(ctx, _AdapterLeakingToken(), conn, blob)
    row = conn.execute("SELECT params::text FROM core.provider_snapshot").fetchone()
    assert row is not None
    assert "should-not-be-stored" not in cast(str, row[0])


@pytest.mark.db
def test_oversized_response_is_stored_in_blob_and_replayable(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    """超过内联上限的响应落对象存储；快照行仍然能完整回放出原始数据。"""
    padding = "x" * (MAX_INLINE_PAYLOAD_BYTES // 10)
    rows = [
        {
            "ts_code": "688256.SH",
            "field": "revenue_total",
            "value": float(i),
            "unit": "CNY",
            "note": padding,
        }
        for i in range(12)
    ]
    ctx = build_asset_context(partition_key=PARTITION)
    adapter = MockFactAdapter(rows)
    records = cast(list[FactRecord], fact_normalized(ctx, adapter, conn, blob))

    snapshot_id = records[0].snapshot_id
    assert snapshot_id is not None
    inline = conn.execute(
        "SELECT response IS NULL, response_ref FROM core.provider_snapshot WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()
    assert inline is not None and inline[0] is True and inline[1] is not None
    replayed = list(adapter.parse(_snapshot_as_raw(conn, snapshot_id, blob)))
    assert len(replayed) == len(rows)
