"""批量嵌入与断点续传。

待办队列就是 `embedding IS NULL AND is_leaf` —— 不需要额外的状态表，
中断后重跑自然只处理剩余部分（docs/05-document-pipeline.md §5.3）。

P1 首次入库有数十万块，托管 API 有速率限制，所以批处理 + 内容哈希缓存
不是优化，是能否完成 P1 的前提（adr/0004 的后果 1）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg

from ragdemo.embed.base import Embedder, content_key, embedding_input, l2_normalize
from ragdemo.embed.cache import PUBLIC_OWNER, EmbeddingCache
from ragdemo.retrieval.vector_index import VectorIndex, VectorItem

DEFAULT_BATCH_SIZE = 64


@dataclass(frozen=True)
class EmbedStats:
    pending: int
    from_cache: int
    computed: int
    written: int
    # 写进向量索引的块数；未传 index 时为 0。加默认值是因为
    # ragdemo/ingest/assets_docs.py 的 block_embeddings 资产已经在用这个
    # dataclass，不能因为加字段就要求所有既有构造点都跟着改。
    indexed: int = 0


def _vector_literal(vector: Sequence[float]) -> str:
    """写库前的最后一道关卡：不管上游算没算归一化，这里强制补一次。

    HNSW 索引用 vector_cosine_ops：一个没归一化的向量混进去不会报错，
    只会静默把排序算错（docs/adr/0004）。`l2_normalize` 是幂等的，
    对已经归一化的向量再算一次不改变结果，代价可以忽略。
    """
    return "[" + ",".join(repr(float(x)) for x in l2_normalize(vector)) + "]"


def embed_pending_blocks(
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    owner_user: str = PUBLIC_OWNER,
    index: VectorIndex | None = None,
    published_after: datetime | None = None,
    published_until: datetime | None = None,
) -> EmbedStats:
    """为尚未嵌入的叶子块生成向量并写回。

    每处理完一批（`batch_size` 个块）就 `commit()` 一次，紧接着（如果传了
    `index`）把同一批写进向量索引。顺序很重要：**先 PG 提交，后写
    Chroma**。反过来会在 PG 回滚时留下 Chroma 里的孤儿向量，而孤儿向量
    正是 ADR-0009 要防的泄漏源——它们不受 PG 事务保护，没有对应的
    core.doc_block 行撤销它们。写向量索引失败不回滚 PG：PG 是真相，
    向量索引是可重建的派生物，重跑本函数会补上。

    **并发安全**：待办队列的 SELECT 带 `FOR UPDATE OF b SKIP LOCKED`，且
    加锁与释放都在同一批次的事务内完成——每一批自己 SELECT、自己
    UPDATE、自己 commit，不是先一次性 SELECT 全部待办、再切片分批处理。
    两个并发跑的分区各自按批次推进时，一方持有行锁的那一批会被另一方的
    SKIP LOCKED 跳过，天然不会选中同一批 block_id 重复计费——这正是本
    函数曾经的洞：旧实现只在最开始做一次不带锁的 SELECT，两个并发调用会
    拿到完全相同的一批 block_id（`ORDER BY block_id LIMIT n` 对两边都是
    确定性的）。批次提交后锁立即释放，与"按批提交缩小崩溃损失面"这个
    目标不冲突——提交本来就是每批发生一次，锁的生命周期只是被收紧到
    与它对齐。

    提交必须显式做：`conn.execute` 在事务块内隐式参与当前事务，`with
    conn.transaction()` 因此只是 SAVEPOINT 而不是顶层事务
    （ragdemo_core/db/migrate.py:61-64 记录过同一个坑）。数十万块的首次
    入库如果中途崩溃，没有显式 commit 的话，已经算过、已经付过 API 费用
    的向量会连同 embedding_cache 一起被整体回滚——断点续传就名不副实了。
    按批提交把损失面从"整个 run"缩小到"最后一个未提交的批"。

    `published_after` / `published_until` 缺省都不过滤（保留旧行为：一次扫
    全部待办队列），必须同时提供或同时省略——两者圈出的是一个
    `(published_after, published_until]` 半开区间。`ragdemo.ingest.
    assets_docs.block_embeddings` 传自己那一分区的抓取窗口（与
    `doc_normalized` 用的是同一个 since/until），把处理范围收窄到"这个
    分区对应的文档产出的块"。

    这里故意不用 `ingested_at`（块被写入 PG 的物理时刻）做收窄：Dagster
    分区的含义是"这个分区代表的业务时间窗口"，而不是"代码碰巧在哪天执行"。
    回填历史分区时两者完全不同——今天回填 2024-10-28 这个分区，写进去的
    行 `ingested_at` 是今天，但这个分区该处理的仍然是 `publish_at` 落在
    2024-10-28 那一天的文档。按 `ingested_at` 收窄会让回填出来的分区永远
    找不到自己该处理的块。
    """
    if (published_after is None) != (published_until is None):
        raise ValueError("published_after 与 published_until 必须同时提供或同时省略")

    # owner_user 在 EmbeddingCache / core.embedding_cache 里的公共分区哨兵是
    # `''`（该表主键三列不能为 NULL，004_documents.sql）；但 core.doc_block
    # 这一列上公共块存的是 NULL（同一份迁移 §5.1 的注释：owner_tenant /
    # owner_user 同时为 NULL 表示公共空间）。两处哨兵不同，查询边界上必须
    # 做一次映射，否则默认调用（owner_user="")会选中全部私有块——
    # 那正是本函数被发现的那个洞（CLAUDE.md §0 用户上传材料私有隔离）。
    doc_block_owner = None if owner_user == PUBLIC_OWNER else owner_user
    cache = EmbeddingCache(conn, model=embedder.model, owner_user=owner_user)

    pending = 0
    computed = 0
    written = 0
    indexed = 0

    while limit is None or written < limit:
        take = batch_size if limit is None else min(batch_size, limit - written)

        with conn.transaction():
            window_filter = (
                " AND b.publish_at > %s AND b.publish_at <= %s "
                if published_after is not None
                else ""
            )
            params: tuple[object, ...] = (
                (doc_block_owner, published_after, published_until, take)
                if published_after is not None
                else (doc_block_owner, take)
            )
            rows = conn.execute(
                "SELECT b.block_id, d.title, b.section_path, b.content_desc, b.content, "
                "       b.doc_id, b.entity_id, b.doc_type, "
                "       b.known_at, b.superseded_at, b.publish_at "
                "  FROM core.doc_block b JOIN core.document d USING (doc_id) "
                " WHERE b.is_leaf AND b.embedding IS NULL "
                "   AND b.owner_user IS NOT DISTINCT FROM %s "
                + window_filter
                + " ORDER BY b.block_id "
                " LIMIT %s "
                " FOR UPDATE OF b SKIP LOCKED",
                params,
            ).fetchall()

            if not rows:
                break

            pending += len(rows)
            chunk_block_ids = [int(r[0]) for r in rows]
            inputs: dict[int, str] = {
                int(r[0]): embedding_input(
                    doc_title=str(r[1] or ""),
                    section_path=str(r[2] or ""),
                    content_desc=str(r[3] or ""),
                    content=str(r[4]),
                )
                for r in rows
            }
            # 向量索引用的元数据：doc_id / entity_id / doc_type / known_at /
            # superseded_at / publish_at，直接取自本批待嵌入块，供写 index 时复用。
            metadata: dict[
                int, tuple[int, str | None, str, datetime, datetime | None, datetime]
            ] = {int(r[0]): (int(r[5]), r[6], str(r[7]), r[8], r[9], r[10]) for r in rows}
            keys = {block_id: content_key(text) for block_id, text in inputs.items()}
            key_to_text = {keys[bid]: text for bid, text in inputs.items()}

            cached = cache.get_many(sorted(set(keys.values())))
            missing = sorted(set(keys.values()) - set(cached.keys()))
            if missing:
                vectors = [
                    l2_normalize(v) for v in embedder.embed([key_to_text[k] for k in missing])
                ]
                new_vectors = dict(zip(missing, vectors, strict=True))
                cache.put_many(new_vectors)
                cached.update(new_vectors)
                computed += len(missing)

            for block_id in chunk_block_ids:
                # embedding_version 与向量同一次 UPDATE 一起写：块从"待嵌入"
                # 变成"已嵌入"这一步，两者必须原子地一起发生，不能有向量已写
                # 但 embedding_version 还是旧值（或反过来）的中间状态
                # （db/migrations/010_block_metadata.sql）。
                conn.execute(
                    "UPDATE core.doc_block SET embedding = %s, embedding_version = %s"
                    " WHERE block_id = %s",
                    (_vector_literal(cached[keys[block_id]]), embedder.model, block_id),
                )
                written += 1
        conn.commit()  # 见函数 docstring：提交同时释放本批的 FOR UPDATE 锁。

        if index is not None:
            items = [
                VectorItem(
                    block_id=block_id,
                    embedding=cached[keys[block_id]],
                    doc_id=metadata[block_id][0],
                    entity_id=metadata[block_id][1],
                    doc_type=metadata[block_id][2],
                    known_at=metadata[block_id][3],
                    superseded_at=metadata[block_id][4],
                    publish_at=metadata[block_id][5],
                )
                for block_id in chunk_block_ids
            ]
            index.upsert(items)
            indexed += len(items)

        if len(rows) < take:
            # 拿到的比要的少：待办队列（在本进程可见、未被其他事务锁住的
            # 范围内）已经见底，没必要再发一轮空查询去确认。
            break

    return EmbedStats(
        pending=pending,
        from_cache=pending - computed,
        computed=computed,
        written=written,
        indexed=indexed,
    )
