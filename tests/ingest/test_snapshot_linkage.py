"""原始响应留存与回放（docs/04-ingestion.md §2.1、CLAUDE.md §1.5）。

每一次真实取数都要先把原始响应写进 `core.provider_snapshot`，再解析；解析出的
每一行事实 / 行情都带 `snapshot_id` 指回那一次响应。验收：任取一行，能从它的
快照重新解析出同一行。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from dagster import build_asset_context

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.tushare import PriceRow, TushareAdapter
from ragdemo.ingest.assets import (
    fact_normalized,
    fin_fact_loaded,
    price_daily_loaded,
    price_normalized,
)
from ragdemo.ingest.assets_docs import doc_normalized, prepare_documents, record_parse_snapshots
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.ingest.prices import PriceSources
from ragdemo.ingest.snapshot import MAX_INLINE_PAYLOAD_BYTES
from ragdemo.ingest.writer import PointInTimeWriter
from ragdemo.parse.chunker import chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.textin import PageBudget, TextInParser
from ragdemo.parse.tree import build_tree
from ragdemo.sources.health import MemorySourceHealth
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
    sources = PriceSources(health=MemorySourceHealth(), tushare_mcp=gateway)
    rows = cast(list[PriceRow], price_normalized(ctx, sources, conn, blob))
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


# --- 公告文档 --------------------------------------------------------------------


@pytest.mark.db
def test_every_announcement_document_points_at_a_replayable_snapshot(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    provider = MockAnnouncementProvider()
    ctx = build_asset_context(partition_key=PARTITION)
    docs = cast(list[NormalizedDocument], doc_normalized(ctx, provider, conn, blob))
    assert docs and all(d.snapshot_id is not None for d in docs)

    writer = DocumentWriter(conn, ingest_run_id="r1", source=provider.provider)
    cfg = ChunkConfig()
    for doc in docs:
        chunks = build_tree(chunk_document(doc, cfg))
        desc = {c.ordinal: "表格描述" for c in chunks if c.block_type == "table"}
        writer.write_document(doc, chunks, desc)

    rows = conn.execute(
        "SELECT source_ref, content_hash, snapshot_id FROM core.document"
    ).fetchall()
    assert rows and all(r[2] is not None for r in rows), "每份文档都必须带 snapshot_id"
    for provider_doc_id, content_hash, snapshot_id in rows:
        replayed = provider.normalize(_snapshot_as_raw(conn, cast(int, snapshot_id), blob))
        assert replayed.provider_doc_id == provider_doc_id
        assert replayed.content_hash == content_hash


# --- TextIn 解析调用 -------------------------------------------------------------


def _path_b_doc(blob: LocalBlobStore) -> NormalizedDocument:
    blob.put("raw/doc-0.pdf", b"%PDF-1.4 fake content 0")
    return NormalizedDocument(
        provider_doc_id="PATH-B-0",
        entity_ref="688256.SH",
        doc_type="annual_report",
        title="路径 B 文档",
        period="2024",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref="raw/doc-0.pdf",
        content_hash="path-b-0",
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=None,
        blocks=[],
    )


@pytest.mark.db
def test_fresh_textin_parse_is_snapshotted_with_cost_and_cache_hits_are_not(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    payload = json.loads(Path("tests/fixtures/xparse/annual_report.json").read_text("utf-8"))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    parser = TextInParser("http://proxy.invalid", blob, transport=httpx.MockTransport(handler))
    doc = _path_b_doc(blob)

    prepared = prepare_documents([doc], parser, blob, PageBudget(10_000), owner_user=None)
    ids = record_parse_snapshots(
        conn, prepared, ingest_run_id="r1", cost_per_page_cny=Decimal("0.05")
    )
    assert len(ids) == 1 and len(calls) == 1

    row = conn.execute(
        "SELECT provider, response, response_ref, cost_cents FROM core.provider_snapshot"
        " WHERE snapshot_id = %s",
        (ids[0],),
    ).fetchone()
    assert row is not None
    provider, response, response_ref, cost_cents = row
    artifacts = prepared[0].artifacts
    assert artifacts is not None
    assert provider == "textin"
    assert response is None, "解析原始 JSON 已在 blob 里，快照只记引用，不重复存"
    assert response_ref == artifacts.json_ref
    assert json.loads(blob.get(cast(str, response_ref))) == payload
    pages = prepared[0].doc.page_count
    assert pages and Decimal(cast(Decimal, cost_cents)) == Decimal(pages) * 5  # 0.05 元 = 5 分

    # 第二次命中缓存：没有新的供应商调用，也就没有新的快照
    again = prepare_documents([doc], parser, blob, PageBudget(10_000), owner_user=None)
    assert record_parse_snapshots(conn, again, ingest_run_id="r2", cost_per_page_cny=None) == []
    assert len(calls) == 1


@pytest.mark.db
def test_parse_cost_is_null_when_price_is_not_configured(
    conn: psycopg.Connection, blob: LocalBlobStore
) -> None:
    payload = json.loads(Path("tests/fixtures/xparse/annual_report.json").read_text("utf-8"))
    parser = TextInParser(
        "http://proxy.invalid",
        blob,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    prepared = prepare_documents(
        [_path_b_doc(blob)], parser, blob, PageBudget(10_000), owner_user=None
    )
    (snapshot_id,) = record_parse_snapshots(
        conn, prepared, ingest_run_id="r1", cost_per_page_cny=None
    )
    row = conn.execute(
        "SELECT cost_cents FROM core.provider_snapshot WHERE snapshot_id = %s", (snapshot_id,)
    ).fetchone()
    assert row is not None and row[0] is None, "单价未知就记 NULL，不能记 0——0 会被当成免费"
