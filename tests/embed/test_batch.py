"""批量嵌入：只处理 embedding IS NULL，命中缓存不重算，可中断可续跑。"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import psycopg
import pytest

from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


class CountingEmbedder(MockEmbedder):
    """统计实际调用次数，用于验证缓存与批大小。"""

    def __init__(self) -> None:
        self.batches = 0
        self.texts = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches += 1
        self.texts += len(texts)
        return super().embed(texts)


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片')"
    )
    c.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
        " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (1,'CN.688256','quarterly','三季报',"
        " '2024-10-28 18:32+08','mock','h1',1,'2024-07-01','2024-10-28 18:32+08','r1')"
    )
    return c


def _add_blocks(conn: psycopg.Connection, contents: list[str], *, is_leaf: bool = True) -> None:
    for i, content in enumerate(contents):
        conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal,"
            " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
            " source, ingest_run_id) "
            "VALUES (1,'paragraph','第一节',%s,%s,%s,'CN.688256','quarterly',"
            " '2024-10-28 18:32+08','2024-07-01','2024-10-28 18:32+08','mock','r1')",
            (i, content, is_leaf),
        )
    conn.commit()


@pytest.mark.db
def test_embeds_all_pending_leaf_blocks(conn: psycopg.Connection) -> None:
    _add_blocks(conn, ["甲", "乙", "丙"])
    stats = embed_pending_blocks(conn, MockEmbedder())
    assert stats.written == 3
    (remaining,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert remaining == 0


@pytest.mark.db
def test_parent_blocks_are_not_embedded(conn: psycopg.Connection) -> None:
    """父块不做嵌入（05 §4.2 规则 4）。"""
    _add_blocks(conn, ["父块内容"], is_leaf=False)
    stats = embed_pending_blocks(conn, MockEmbedder())
    assert stats.pending == 0
    assert stats.written == 0


@pytest.mark.db
def test_rerun_after_interruption_only_handles_remaining(conn: psycopg.Connection) -> None:
    """断点续传：第一次只处理 2 条，第二次处理剩下的。"""
    _add_blocks(conn, ["甲", "乙", "丙", "丁"])
    first = embed_pending_blocks(conn, MockEmbedder(), limit=2)
    second = embed_pending_blocks(conn, MockEmbedder())
    assert (first.written, second.written) == (2, 2)


@pytest.mark.db
def test_identical_content_hits_cache_and_is_computed_once(conn: psycopg.Connection) -> None:
    """各家公告中大量重复的模板段落只算一次。"""
    _add_blocks(conn, ["完全相同的模板段落"] * 5)
    embedder = CountingEmbedder()
    stats = embed_pending_blocks(conn, embedder)
    assert stats.written == 5
    assert embedder.texts == 1, "相同内容只应调用一次嵌入"
    assert stats.from_cache == 4


@pytest.mark.db
def test_batch_size_is_respected(conn: psycopg.Connection) -> None:
    _add_blocks(conn, [f"内容{i}" for i in range(10)])
    embedder = CountingEmbedder()
    embed_pending_blocks(conn, embedder, batch_size=3)
    assert embedder.batches == 4  # 3 + 3 + 3 + 1


@pytest.mark.db
def test_written_vectors_are_normalised(conn: psycopg.Connection) -> None:
    _add_blocks(conn, ["甲"])
    embed_pending_blocks(conn, MockEmbedder())
    # `<#>` 是 pgvector 的负内积算子，优先级低于 `*`——不加括号会被解析成
    # `embedding <#> (embedding * -1)`，而 `vector * integer` 根本没有这个
    # 重载，报 UndefinedFunction。显式加括号：先算内积再取负，对已归一化
    # 的向量得到 1.0（自己与自己的内积 = 模长的平方 = 1）。
    (norm,) = conn.execute(
        "SELECT round(((embedding <#> embedding) * -1)::numeric, 4) FROM core.doc_block"
        " WHERE embedding IS NOT NULL"
    ).fetchone()  # type: ignore[misc]
    assert float(norm) == pytest.approx(1.0, abs=1e-3)
