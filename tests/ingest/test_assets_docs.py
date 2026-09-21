"""文档管线端到端：Mock 公告 → 解析 → 切块 → 入库 → 嵌入，全部块可检索。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from dagster import build_asset_context

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.embed.batch import EmbedStats
from ragdemo.embed.mock import MockEmbedder
from ragdemo.ingest.assets_docs import (
    block_embeddings,
    doc_blocks_loaded,
    doc_normalized,
    prepare_documents,
)
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.parse.textin import (
    MockDocumentParser,
    PageBudget,
    ParsePermanent,
    ParseResult,
    TextInParser,
    artifact_keys,
)
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
XPARSE_FIXTURE = Path("tests/fixtures/xparse/annual_report.json")


def _xparse_payload() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(XPARSE_FIXTURE.read_text(encoding="utf-8")))


def _normalized(ctx: Any) -> list[NormalizedDocument]:
    """`doc_normalized` 是 `@asset` 装饰的函数：Dagster 的 `AssetsDefinition.__call__`
    对直接调用一律标注返回 `object`（不看被装饰函数的真实签名，见
    `dagster._core.definitions.assets.definition.assets_definition.AssetsDefinition.__call__`）。
    这里把结果转回调用方实际拿到的类型，不是加宽被测代码的类型。`ctx` 标 `Any` 是
    因为 `build_asset_context()` 返回的 `DirectAssetExecutionContext` 不在 dagster
    顶层导出里，没有必要为一个测试内部小工具去引用它的私有模块路径。"""
    return cast("list[NormalizedDocument]", doc_normalized(ctx, MockAnnouncementProvider()))


def _path_b_docs(blob: LocalBlobStore, *, count: int = 1) -> list[NormalizedDocument]:
    """构造路径 B 形态的文档：blocks=[]，raw_bytes_ref 指向 blob 里的原始字节。

    MockAnnouncementProvider（Task 8）目前两份样例文档的 blocks 都非空——
    全部是路径 A 形态，测试套件里没有任何一份路径 B 文档可用。本任务要测
    的恰恰是路径 B（送 xParse 解析）的分流逻辑，所以这里直接构造，
    不依赖 MockAnnouncementProvider 的样例数据形状。
    """
    docs = []
    for i in range(count):
        ref = f"raw/doc-{i}.pdf"
        blob.put(ref, f"%PDF-1.4 fake content {i}".encode())
        docs.append(
            NormalizedDocument(
                provider_doc_id=f"PATH-B-{i}",
                entity_ref="688256.SH",
                doc_type="annual_report",
                title=f"路径 B 文档 {i}",
                period="2024",
                publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
                language="zh",
                source_url=None,
                raw_bytes_ref=ref,
                content_hash=f"hash-{i}",
                is_correction=False,
                supersedes_provider_doc_id=None,
                page_count=None,
                blocks=[],
            )
        )
    return docs


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    c.commit()
    return c


@pytest.mark.db
def test_pipeline_produces_searchable_blocks(conn: psycopg.Connection, tmp_path: Path) -> None:
    ctx = build_asset_context(partition_key="2024-10-28")
    docs = _normalized(ctx)
    prepared = prepare_documents(
        docs, MockDocumentParser(), LocalBlobStore(tmp_path), PageBudget(1000)
    )
    writer = DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")
    doc_blocks_loaded(ctx, prepared, writer)
    stats = cast(EmbedStats, block_embeddings(ctx, conn, MockEmbedder()))

    assert stats.written > 0
    (leaves_without_vec,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert leaves_without_vec == 0

    (bm25_hits,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE content @@@ '云端训练芯片'"
    ).fetchone()  # type: ignore[misc]
    assert bm25_hits > 0


@pytest.mark.db
def test_rerunning_the_pipeline_is_idempotent(conn: psycopg.Connection, tmp_path: Path) -> None:
    ctx = build_asset_context(partition_key="2024-10-28")
    docs = _normalized(ctx)
    blob = LocalBlobStore(tmp_path)
    prepared = prepare_documents(docs, MockDocumentParser(), blob, PageBudget(1000))

    doc_blocks_loaded(
        ctx, prepared, DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")
    )
    (after_first,) = conn.execute("SELECT count(*) FROM core.doc_block").fetchone()  # type: ignore[misc]

    doc_blocks_loaded(
        ctx, prepared, DocumentWriter(conn, ingest_run_id="r2", source="mock-announcements")
    )
    (after_second,) = conn.execute("SELECT count(*) FROM core.doc_block").fetchone()  # type: ignore[misc]

    assert after_first == after_second


@pytest.mark.db
def test_parse_artifact_refs_land_in_the_document_row(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """用户要的「存 JSON + 存 Markdown」在库里的落点就是这两列（05 §2.4）。

    用真实的 TextInParser（缓存命中路径，不发网络请求）而不是
    MockDocumentParser：Mock 的 ParseResult 本来就没有 json_ref/md_ref
    ——它是给切块/入库测试用的占位符，从来不落盘，测不出这两列的写入路径。
    """
    ctx = build_asset_context(partition_key="2024-10-28")
    blob = LocalBlobStore(tmp_path)
    docs = _path_b_docs(blob)
    parser = TextInParser("http://proxy.invalid", blob)
    raw_bytes = blob.get(docs[0].raw_bytes_ref)  # type: ignore[arg-type]
    json_key, _ = artifact_keys(parser.content_hash(raw_bytes), parser.param_fp)
    blob.put(json_key, json.dumps(_xparse_payload(), ensure_ascii=False).encode("utf-8"))

    prepared = prepare_documents(docs, parser, blob, PageBudget(1000))
    doc_blocks_loaded(ctx, prepared, DocumentWriter(conn, ingest_run_id="r1", source="mock"))

    rows = conn.execute(
        "SELECT parse_engine, parse_json_ref, parse_md_ref FROM core.document"
    ).fetchall()
    assert rows
    for engine, json_ref, md_ref in rows:
        assert engine.startswith("mock:") or engine.startswith("textin:")
        assert json_ref is not None and md_ref is not None


def test_path_a_documents_are_not_re_parsed(tmp_path: Path) -> None:
    """供应商已经给了结构化块，再送去解析既花钱又不如原件准（05 §1）。"""
    class Exploding(MockDocumentParser):
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
            raise AssertionError("路径 A 的文档不应该被解析")

    ctx = build_asset_context(partition_key="2024-10-28")
    docs = [d for d in _normalized(ctx) if d.blocks]
    prepared = prepare_documents(docs, Exploding(), LocalBlobStore(tmp_path), PageBudget(1000))

    assert [p.artifacts for p in prepared] == [None] * len(prepared)


def test_budget_exhaustion_stops_before_the_next_call(tmp_path: Path) -> None:
    """页数预算耗尽就停，不静默烧钱（05 §2.7）。"""
    class Counting(MockDocumentParser):
        calls = 0

        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
            type(self).calls += 1
            return super().parse(file_bytes, owner_user=owner_user)

    blob = LocalBlobStore(tmp_path)
    docs = _path_b_docs(blob, count=2)
    parser = Counting()
    prepare_documents(docs, parser, blob, PageBudget(1))

    assert Counting.calls == 1  # 第一份用掉 2 页，预算见底，第二份不再发


def test_permanent_failure_still_records_the_document(tmp_path: Path) -> None:
    """不留记号的话，下次分区重跑会再拉一遍、再失败一遍（05 §2.6）。"""
    class Unsupported(MockDocumentParser):
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
            raise ParsePermanent("unsupported", 40303)

    blob = LocalBlobStore(tmp_path)
    docs = _path_b_docs(blob)
    prepared = prepare_documents(docs, Unsupported(), blob, PageBudget(1000))

    assert prepared
    for item in prepared:
        assert item.doc.blocks == []
        assert item.artifacts is not None
        assert item.artifacts.engine == "textin:unsupported"
