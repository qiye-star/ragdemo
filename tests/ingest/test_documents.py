"""文档入库：反规范化列一致、整份回滚、重解析不改 known_at。"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
import pytest

from ragdemo.adapters.announcements import NormalizedDocument
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
    w = DocumentWriter(conn, ingest_run_id="r1", source="mock", disclosure_lag=timedelta(days=1))
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    w.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    publish_at, known_at = conn.execute("SELECT publish_at, known_at FROM core.document").fetchone()  # type: ignore[misc]
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
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )
    new_known_at, version_group, supersedes = writer.conn.execute(
        "SELECT known_at, version_group_id, supersedes_doc_id FROM core.document WHERE doc_id = %s",
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


# --- 提交必须真的发生，不能只是"同一个连接自己能看见自己的写入" ------------


@pytest.mark.db
def test_write_document_commits_and_is_visible_from_another_connection(
    writer: DocumentWriter, temp_db: str
) -> None:
    """`write_document` 在 `_live_doc_id` 那次 SELECT 时就已经隐式开了外层
    事务，内部的 `with self.conn.transaction()` 因此只是 SAVEPOINT——没有
    显式 commit 的话，写进去的文档与块只有在同一个连接上读得到，换一个
    连接就什么都看不到，进程一崩溃就真的全丢。所有读回断言都在 writer.conn
    这同一个连接上做的话，测不出这个问题（同一事务内自己当然能看见自己
    未提交的写入）——必须换一个独立连接。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    other_conn = psycopg.connect(temp_db)
    try:
        (doc_count,) = other_conn.execute(
            "SELECT count(*) FROM core.document WHERE doc_id = %s", (result.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert doc_count == 1
        (block_count,) = other_conn.execute(
            "SELECT count(*) FROM core.doc_block WHERE doc_id = %s", (result.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert block_count == len(result.block_ids) > 0
    finally:
        other_conn.close()


@pytest.mark.db
def test_reparse_document_commits_and_is_visible_from_another_connection(
    writer: DocumentWriter, temp_db: str
) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    second = writer.reparse_document(
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )

    other_conn = psycopg.connect(temp_db)
    try:
        (superseded_at,) = other_conn.execute(
            "SELECT superseded_at FROM core.document WHERE doc_id = %s", (first.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert superseded_at is not None
        (new_doc_count,) = other_conn.execute(
            "SELECT count(*) FROM core.document WHERE doc_id = %s", (second.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert new_doc_count == 1
    finally:
        other_conn.close()


# --- Finding C: 去重检查要只认活着的行 -------------------------------------


@pytest.mark.db
def test_reingest_after_reparse_returns_the_live_doc_id(writer: DocumentWriter) -> None:
    """重解析后，同一个 (source, content_hash) 对应两行：被取代的旧行与新的
    活跃行——原始字节没变，变的只是解析结果。再次摄入同一份原始文档时，
    去重检查必须只认 superseded_at IS NULL 的那一行；否则可能把旧行的
    doc_id 当作「已存在，跳过」返回——那个 doc_id 在 asof.document 里不可见，
    调用方一旦信了 DocumentWriteResult.doc_id 就会引用一份死文档。
    """
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    reparsed = writer.reparse_document(
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )

    again = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    assert again.skipped is True
    assert again.doc_id == reparsed.doc_id
    assert again.doc_id != first.doc_id


# --- Finding D: 并发首次写入要降级为 skipped，不能崩溃 ----------------------


@pytest.mark.db
def test_concurrent_first_write_degrades_to_skipped(
    writer: DocumentWriter, temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两份从未见过的文档并发写入：两条连接都在各自的存在性检查里判定
    "不存在"，然后都去插入——分区唯一索引 document_dedup_uk
    （008_document_dedup_partial.sql）保证数据不会重复，但落败的一方原本
    会拿到未处理的 UniqueViolation，直接把整个 run 炸掉（CLAUDE.md §1.6：
    并发摄入同一条新公告是增量触发模型下的正常路径，不是例外）。

    用真实的第二个连接重放这个竞态窗口，而不是起线程：真正让两条连接在
    INSERT 语句上互相阻塞、再解开需要真的并发（线程/协程）；这里改成对
    时序的确定性重放——先在 conn_b 上做一次真实的存在性检查（确认此刻
    确实不存在），再让 conn_a 完整写入并提交（赢家），然后把 conn_b
    "写入前的那次"存在性检查结果钉在刚才真实观察到的"不存在"，逼它跟
    真並发场景一样往下走到 INSERT。except 分支里恢复用的第二次存在性
    检查不打补丁——用真实、当下的数据，因为落败方必须能看到赢家刚提交
    的活跃行，这也是为什么本测试特意不用 REPEATABLE READ 事务隔离去模拟
    竞态：那样会让 except 里的重新查询也困在旧快照里，读不到赢家的提交，
    而这个模块实际使用的连接从未设置过非默认隔离级别（一律是 Postgres
    默认的 READ COMMITTED），钉住存在性检查的返回值比改变隔离级别更贴近
    真实的并发场景。
    """
    doc = _docs()[0]
    chunks, desc = _prepare(doc)

    content_hash = cast(NormalizedDocument, doc).content_hash

    conn_b = psycopg.connect(temp_db)
    writer_b = DocumentWriter(conn_b, ingest_run_id="r2", source="mock-announcements")

    # conn_b 此刻真实检查一次：这份文档确实还不存在。
    assert writer_b._live_doc_id(content_hash) is None

    # conn_a（赢家）完整写入并提交。
    winner = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    writer.conn.commit()
    assert winner.skipped is False

    # 把 conn_b 第一次存在性检查的结果钉在刚才观察到的"不存在"，模拟它的
    # 检查发生在赢家提交之前；之后（异常处理里恢复用的那次）放行到真实实现。
    real_live_doc_id = writer_b._live_doc_id
    call_count = 0

    def _stale_first_check(content_hash: str) -> int | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return real_live_doc_id(content_hash)

    monkeypatch.setattr(writer_b, "_live_doc_id", _stale_first_check)

    loser = writer_b.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    assert loser.skipped is True
    assert loser.doc_id == winner.doc_id
    assert loser.block_ids == []
    (n,) = writer.conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert n == 1


# --- Finding E: 实体解析失败要留痕，不能悄无声息 ----------------------------


@pytest.mark.db
def test_unresolved_entity_ref_still_writes_and_warns(
    writer: DocumentWriter, caplog: pytest.LogCaptureFixture
) -> None:
    """entity_ref 非空但在 core.entity 里找不到匹配行（典型场景：新上市实体
    还没有回填代码列）：文档必须照常写入——拒绝会在增量触发模型下
    （CLAUDE.md §1.6）一直挡住这个实体的全部公告，直到有人手工建好
    core.entity 行。但也不能像"政策/宏观文档没有 entity_ref"那样悄无声息，
    这是数据质量问题，需要有人能看到。
    """
    doc = replace(
        cast(NormalizedDocument, _docs()[0]),
        entity_ref="NOT-A-REAL-CODE",
        content_hash="unresolved-entity-hash",
    )
    chunks, desc = _prepare(doc)

    with caplog.at_level(logging.WARNING, logger="ragdemo.ingest.documents"):
        result = writer.write_document(doc, chunks, desc)

    assert result.skipped is False
    (entity_id,) = writer.conn.execute(
        "SELECT entity_id FROM core.document WHERE doc_id = %s", (result.doc_id,)
    ).fetchone()  # type: ignore[misc]
    assert entity_id is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("entity_ref" in r.getMessage() for r in warnings)
    assert any(getattr(r, "entity_ref", None) == "NOT-A-REAL-CODE" for r in warnings)
    assert any(getattr(r, "ingest_run_id", None) == "r1" for r in warnings)
