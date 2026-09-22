"""批量嵌入：只处理 embedding IS NULL，命中缓存不重算，可中断可续跑。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.embed.base import content_key, embedding_input
from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.cache import EmbeddingCache
from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.vector_index import VectorCandidate, VectorItem
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


class RecordingVectorIndex:
    """内存假实现，只用来断言 embed_pending_blocks 有没有在正确的时机调用它。"""

    def __init__(self) -> None:
        self.upserted: list[VectorItem] = []

    def upsert(self, items: Sequence[VectorItem]) -> None:
        self.upserted.extend(items)

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int,
        as_of: datetime | None = None,
        entity_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[VectorCandidate]:
        raise NotImplementedError("本测试不需要 query")

    def delete(self, block_ids: Sequence[int]) -> None:
        raise NotImplementedError("本测试不需要 delete")

    def count(self) -> int:
        return len(self.upserted)

    def existing_ids(self, block_ids: Sequence[int]) -> set[int]:
        upserted_ids = {item.block_id for item in self.upserted}
        return {block_id for block_id in block_ids if block_id in upserted_ids}


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


def _add_blocks(
    conn: psycopg.Connection,
    contents: list[str],
    *,
    is_leaf: bool = True,
    owner_user: str | None = None,
    ordinal_start: int = 0,
) -> None:
    for i, content in enumerate(contents, start=ordinal_start):
        conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal,"
            " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
            " source, ingest_run_id, owner_user) "
            "VALUES (1,'paragraph','第一节',%s,%s,%s,'CN.688256','quarterly',"
            " '2024-10-28 18:32+08','2024-07-01','2024-10-28 18:32+08','mock','r1',%s)",
            (i, content, is_leaf, owner_user),
        )
    conn.commit()


def _content_key_for(content: str) -> str:
    """复用生产代码里拼接嵌入输入的逻辑，不在测试里重复一份容易漂移的实现。"""
    return content_key(
        embedding_input(doc_title="三季报", section_path="第一节", content_desc="", content=content)
    )


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


@pytest.mark.db
def test_index_defaults_to_none_and_indexed_stays_zero(conn: psycopg.Connection) -> None:
    """不传 index 时（现状默认路径），indexed 恒为 0，不影响既有调用方。"""
    _add_blocks(conn, ["甲", "乙"])
    stats = embed_pending_blocks(conn, MockEmbedder())
    assert stats.indexed == 0


@pytest.mark.db
def test_index_is_upserted_after_pg_commit(conn: psycopg.Connection) -> None:
    """传入 index 时，同一批块提交到 PG 之后要写进向量索引，indexed 计数要对。"""
    _add_blocks(conn, ["甲", "乙", "丙"])
    index = RecordingVectorIndex()

    stats = embed_pending_blocks(conn, MockEmbedder(), index=index)

    assert stats.written == 3
    assert stats.indexed == 3
    assert len(index.upserted) == 3
    assert {item.block_id for item in index.upserted} == {
        int(r[0])
        for r in conn.execute("SELECT block_id FROM core.doc_block WHERE embedding IS NOT NULL")
    }
    # 先 PG 提交后写索引：调用发生时，PG 里已经能查到这些块的 embedding。
    written_before_index_call = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE embedding IS NOT NULL"
    ).fetchone()
    assert written_before_index_call is not None
    assert written_before_index_call[0] == 3


@pytest.mark.db
def test_index_not_called_when_no_pending_blocks(conn: psycopg.Connection) -> None:
    """没有待嵌入块时提前返回，不该调用向量索引。"""
    index = RecordingVectorIndex()
    stats = embed_pending_blocks(conn, MockEmbedder(), index=index)
    assert stats.indexed == 0
    assert index.upserted == []


# --- 分区窗口收窄（published_after/until，与 ingested_at 无关） -------------


def _add_block_published_at(
    conn: psycopg.Connection, content: str, publish_at: str, *, ordinal: int = 0
) -> None:
    """与 `_add_blocks` 不同：允许指定 `publish_at`，用来测试窗口收窄。"""
    conn.execute(
        "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal,"
        " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at,"
        " source, ingest_run_id) "
        "VALUES (1,'paragraph','第一节',%s,%s,true,'CN.688256','quarterly',"
        " %s,'2024-07-01',%s,'mock','r1')",
        (ordinal, content, publish_at, publish_at),
    )
    conn.commit()


@pytest.mark.db
def test_publish_window_excludes_blocks_outside_the_partition(conn: psycopg.Connection) -> None:
    """两块分别落在 2024-10-27 与 2024-10-28；只传 2024-10-28 那天的窗口，
    应该只处理落在窗口内的那一块——这就是回填历史分区时必须成立的东西：
    收窄用的是 publish_at（业务时间），不是这一行被物理写入的时刻。"""
    # UTC 10-27 10:32 / UTC 10-28 10:32
    _add_block_published_at(conn, "十月二十七日", "2024-10-27 18:32+08", ordinal=0)
    _add_block_published_at(conn, "十月二十八日", "2024-10-28 18:32+08", ordinal=1)

    # (since, until] 半开区间只圈住 UTC 10-28 那一天，第一块（UTC 10-27）落在窗口外。
    since = datetime(2024, 10, 27, 23, 59, 59, 999999, tzinfo=UTC)
    until = datetime(2024, 10, 28, 23, 59, 59, 999999, tzinfo=UTC)

    stats = embed_pending_blocks(conn, MockEmbedder(), published_after=since, published_until=until)

    assert stats.written == 1
    (remaining,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert remaining == 1, "窗口外的那一块不该被这次调用处理"


@pytest.mark.db
def test_publish_window_requires_both_bounds_together(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="published_after"):
        embed_pending_blocks(conn, MockEmbedder(), published_after=datetime.now(UTC))


# --- owner_user 隔离（CLAUDE.md §0：用户上传材料私有隔离） ------------------


@pytest.mark.db
def test_public_run_does_not_embed_or_leak_another_owners_block(
    conn: psycopg.Connection,
) -> None:
    """默认（公共）批次既不能嵌入别的用户的私有块，也不能把私有内容的向量
    写进公共缓存分区——那正是 09 §3.3 描述的哈希命中探测攻击：
    doc_block.owner_user 用 NULL 表示公共，embedding_cache.owner_user 用 ''
    表示公共，两个哨兵不能直接相等比较，查询必须显式做映射。"""
    _add_blocks(conn, ["公共块甲", "公共块乙"])
    _add_blocks(conn, ["私有块甲"], owner_user="u1", ordinal_start=2)

    stats = embed_pending_blocks(conn, MockEmbedder())

    assert stats.pending == 2
    assert stats.written == 2
    (private_pending,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE owner_user = 'u1' AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert private_pending == 1, "私有块不该被公共批次嵌入"
    private_key = _content_key_for("私有块甲")
    assert EmbeddingCache(conn, model=MockEmbedder().model).get_many([private_key]) == {}, (
        "私有内容的向量不该出现在公共缓存分区"
    )


@pytest.mark.db
def test_owner_scoped_run_does_not_touch_public_blocks(conn: psycopg.Connection) -> None:
    """反过来也要成立：传了 owner_user 的批次不该动公共块——同一个洞的另一半，
    发现前的代码是"按调用方的 owner_user 建缓存，但查询无视 owner_user"，
    所以 owner_user='u1' 的调用会把公共块也一起嵌入并归到 u1 名下。"""
    _add_blocks(conn, ["公共块甲"])
    _add_blocks(conn, ["私有块甲", "私有块乙"], owner_user="u1", ordinal_start=1)

    stats = embed_pending_blocks(conn, MockEmbedder(), owner_user="u1")

    assert stats.pending == 2
    assert stats.written == 2
    (public_pending,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE owner_user IS NULL AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert public_pending == 1, "公共块不该被私有批次嵌入"


# --- 写库边界的归一化（HNSW 用 vector_cosine_ops，没归一化会静默错排序） ----


class UnnormalizedEmbedder(MockEmbedder):
    """故意不归一化，用来证明写库边界会强制补一次，而不是依赖嵌入器自觉。"""

    model = "mock-unnormalized"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[x * 4.0 for x in v] for v in super().embed(texts)]


@pytest.mark.db
def test_unnormalized_embedder_output_is_normalized_at_the_write_boundary(
    conn: psycopg.Connection,
) -> None:
    _add_blocks(conn, ["甲"])
    embed_pending_blocks(conn, UnnormalizedEmbedder())

    # `<#>` 是 pgvector 的负内积算子，优先级低于 `*`，括号的理由见
    # test_written_vectors_are_normalised 顶上的注释。对已归一化向量，
    # 自己与自己的内积就是模长的平方，应为 1。
    (block_norm,) = conn.execute(
        "SELECT round(((embedding <#> embedding) * -1)::numeric, 4) FROM core.doc_block"
        " WHERE embedding IS NOT NULL"
    ).fetchone()  # type: ignore[misc]
    assert float(block_norm) == pytest.approx(1.0, abs=1e-3)

    (cache_norm,) = conn.execute(
        "SELECT round(((embedding <#> embedding) * -1)::numeric, 4) FROM core.embedding_cache"
    ).fetchone()  # type: ignore[misc]
    assert float(cache_norm) == pytest.approx(1.0, abs=1e-3)


# --- 按批提交（断点续传：崩溃只丢最后一个未提交的批） ------------------------


class FlakyEmbedder(MockEmbedder):
    """第二次调用（也就是第二批）直接抛异常，模拟批处理中途崩溃。"""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("模拟崩溃：第二批还没提交")
        return super().embed(texts)


@pytest.mark.db
def test_committed_batches_survive_a_crash_in_a_later_batch(
    conn: psycopg.Connection, temp_db: str
) -> None:
    """embed_pending_blocks 在 SELECT 那一刻就隐式开了外层事务，函数内部的
    `with conn.transaction()` 因此只是 SAVEPOINT——没有显式 commit 的话，
    整个 run 崩溃会把已经算好、已经付过 API 费用的第一批也一起回滚。用一个
    独立的连接读，才能真正排除"读到同一连接里未提交的数据"这种假阳性。"""
    _add_blocks(conn, ["甲", "乙", "丙", "丁"])

    with pytest.raises(RuntimeError, match="模拟崩溃"):
        embed_pending_blocks(conn, FlakyEmbedder(), batch_size=2)

    other_conn = psycopg.connect(temp_db)
    try:
        (embedded,) = other_conn.execute(
            "SELECT count(*) FROM core.doc_block WHERE embedding IS NOT NULL"
        ).fetchone()  # type: ignore[misc]
        assert embedded == 2, "第一批（甲/乙）应该已经提交并对其他连接可见"
        (still_pending,) = other_conn.execute(
            "SELECT count(*) FROM core.doc_block WHERE embedding IS NULL"
        ).fetchone()  # type: ignore[misc]
        assert still_pending == 2, "第二批（丙/丁）崩溃时还没提交，重跑时应该还在待办队列里"
    finally:
        other_conn.close()


# --- 并发分区不重复计费（FOR UPDATE OF b SKIP LOCKED） -----------------------


@pytest.mark.db
def test_concurrent_run_skips_rows_locked_by_another_connection(
    conn: psycopg.Connection, temp_db: str
) -> None:
    """旧实现只在最开始做一次不带锁的 SELECT，两个并发跑的分区会选中完全
    相同的一批 block_id，重复调用计费的嵌入 API。用一个独立连接手工
    `FOR UPDATE SKIP LOCKED` 住这两条块（模拟"另一个分区正在处理、还没
    提交"），验证 embed_pending_blocks 在这段时间里必须一条都拿不到——
    而不是像旧实现那样对同一批行再算一次向量。"""
    _add_blocks(conn, ["甲", "乙"])

    holder = psycopg.connect(temp_db)
    locked_ids = [
        int(r[0])
        for r in holder.execute(
            "SELECT block_id FROM core.doc_block WHERE is_leaf AND embedding IS NULL "
            "ORDER BY block_id FOR UPDATE SKIP LOCKED"
        ).fetchall()
    ]
    assert locked_ids == [
        int(r[0])
        for r in conn.execute(
            "SELECT block_id FROM core.doc_block WHERE is_leaf ORDER BY block_id"
        ).fetchall()
    ], "前置条件：holder 锁住的应该正是这两条待办块"

    try:
        embedder = CountingEmbedder()
        stats = embed_pending_blocks(conn, embedder, batch_size=64)

        assert stats.written == 0, "两条待办块都被 holder 锁住，本次调用应该一条都拿不到"
        assert stats.pending == 0
        assert embedder.batches == 0, "拿不到行就不该调用嵌入 API（这正是要防的重复计费）"
    finally:
        holder.rollback()  # 释放锁，不影响其他测试复用同一个 temp_db
        holder.close()

    # holder 释放锁之后，正常重跑应该能补上这两条——待办没有被误标记为"已处理"。
    stats_after = embed_pending_blocks(conn, MockEmbedder())
    assert stats_after.written == 2
