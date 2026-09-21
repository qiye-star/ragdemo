"""文档入库：反规范化列一致、整份回滚、重解析不改 known_at。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.ingest.documents import DocumentWriter, MetadataInvalid
from ragdemo.parse.chunker import Chunk, chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.tree import build_tree
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
CFG = ChunkConfig()


def _docs() -> list[object]:
    p = MockAnnouncementProvider()
    ctx = FetchContext(ingest_run_id="t", partition_date=datetime.now(UTC).date())
    return [
        p.normalize(raw)
        for raw in p.list_documents(
            ctx, since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]


def _prepare(doc: object) -> tuple[list[Chunk], dict[int, str]]:
    chunks = build_tree(chunk_document(doc, CFG))  # type: ignore[arg-type]
    describer = MockTableDescriber()
    descriptions = {
        c.ordinal: describer.describe(c.content, title="t", section_path=c.section_path)
        for c in chunks
        if c.block_type == "table"
    }
    return chunks, descriptions


@pytest.fixture()
def writer(temp_db: str) -> DocumentWriter:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.commit()
    return DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")


@pytest.mark.db
def test_document_and_blocks_are_written(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    assert result.doc_id > 0
    assert len(result.block_ids) == len(chunks)


@pytest.mark.db
def test_denormalised_columns_match_the_document(writer: DocumentWriter) -> None:
    """02 §5.3 的反规范化列必须与 document 完全一致，否则检索过滤会错。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    (mismatches,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.entity_id IS DISTINCT FROM d.entity_id"
        "    OR b.doc_type IS DISTINCT FROM d.doc_type"
        "    OR b.known_at IS DISTINCT FROM d.known_at"
        "    OR b.publish_at IS DISTINCT FROM d.publish_at"
    ).fetchone()  # type: ignore[misc]
    assert mismatches == 0


@pytest.mark.db
def test_known_at_applies_disclosure_lag(temp_db: str) -> None:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.commit()
    w = DocumentWriter(
        conn, ingest_run_id="r1", source="mock", disclosure_lag=timedelta(days=1)
    )
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    w.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    publish_at, known_at = conn.execute(
        "SELECT publish_at, known_at FROM core.document"
    ).fetchone()  # type: ignore[misc]
    assert known_at - publish_at == timedelta(days=1)


@pytest.mark.db
def test_duplicate_content_hash_is_skipped(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    second = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    assert second.skipped is True
    (n,) = writer.conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_invalid_metadata_rolls_back_the_whole_document(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    # 去掉第一块（build_tree 输出「父块在前」，ordinal 0 是某个父块）：
    # 剩余块的 ordinal 集合变成 {1,...,N-1}，长度 N-1，不再是 range(N-1)，
    # 触发「ordinal 不连续」。去掉最后一块反而测不出问题——build_tree 的
    # 输出天然以最大 ordinal 结尾，截掉它只是把序列缩短成仍然连续的
    # range(N-1)，validate_chunks 检测不到任何异常。
    broken = [*chunks[1:]]
    with pytest.raises(MetadataInvalid):
        writer.write_document(doc, broken, desc)  # type: ignore[arg-type]
    (docs, blocks) = writer.conn.execute(
        "SELECT (SELECT count(*) FROM core.document), (SELECT count(*) FROM core.doc_block)"
    ).fetchone()  # type: ignore[misc]
    assert (docs, blocks) == (0, 0)


@pytest.mark.db
def test_reparse_keeps_original_known_at(writer: DocumentWriter) -> None:
    """重解析不改变这份文档在现实中何时可知——改了它就会从历史回测中消失。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    (original_known_at,) = writer.conn.execute(
        "SELECT known_at FROM core.document WHERE doc_id = %s", (first.doc_id,)
    ).fetchone()  # type: ignore[misc]

    second = writer.reparse_document(
        doc, chunks, desc, supersedes_doc_id=first.doc_id  # type: ignore[arg-type]
    )
    new_known_at, version_group, supersedes = writer.conn.execute(
        "SELECT known_at, version_group_id, supersedes_doc_id FROM core.document"
        " WHERE doc_id = %s",
        (second.doc_id,),
    ).fetchone()  # type: ignore[misc]

    assert new_known_at == original_known_at
    assert version_group == first.doc_id
    assert supersedes == first.doc_id


@pytest.mark.db
def test_reparse_marks_old_blocks_superseded_but_keeps_them(writer: DocumentWriter) -> None:
    """旧块必须保留——历史观点的 evidence_blocks 引用着它们。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    writer.reparse_document(doc, chunks, desc, supersedes_doc_id=first.doc_id)  # type: ignore[arg-type]

    (old_alive,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE doc_id = %s", (first.doc_id,)
    ).fetchone()  # type: ignore[misc]
    (old_superseded,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE doc_id = %s AND superseded_at IS NOT NULL",
        (first.doc_id,),
    ).fetchone()  # type: ignore[misc]
    assert old_alive == old_superseded > 0
