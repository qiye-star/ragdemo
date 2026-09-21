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
) -> EmbedStats:
    """为尚未嵌入的叶子块生成向量并写回。

    每处理完一批（`batch_size` 个块）就 `commit()` 一次，紧接着（如果传了
    `index`）把同一批写进向量索引。顺序很重要：**先 PG 提交，后写
    Chroma**。反过来会在 PG 回滚时留下 Chroma 里的孤儿向量，而孤儿向量
    正是 ADR-0009 要防的泄漏源——它们不受 PG 事务保护，没有对应的
    core.doc_block 行撤销它们。写向量索引失败不回滚 PG：PG 是真相，
    向量索引是可重建的派生物，重跑本函数会补上。

    提交必须显式做：`conn.execute` 已经在本函数最上面的 SELECT 那一刻
    隐式开了一个外层事务，下面的 `with conn.transaction()` 因此只是
    SAVEPOINT 而不是顶层事务（ragdemo_core/db/migrate.py:61-64 记录过
    同一个坑）。数十万块的首次入库如果中途崩溃，没有显式 commit 的话，
    已经算过、已经付过 API 费用的向量会连同 embedding_cache 一起被整体
    回滚——断点续传就名不副实了。按批提交把损失面从"整个 run"缩小到
    "最后一个未提交的批"。
    """
    # owner_user 在 EmbeddingCache / core.embedding_cache 里的公共分区哨兵是
    # `''`（该表主键三列不能为 NULL，004_documents.sql）；但 core.doc_block
    # 这一列上公共块存的是 NULL（同一份迁移 §5.1 的注释：owner_tenant /
    # owner_user 同时为 NULL 表示公共空间）。两处哨兵不同，查询边界上必须
    # 做一次映射，否则默认调用（owner_user="")会选中全部私有块——
    # 那正是本函数被发现的那个洞（CLAUDE.md §0 用户上传材料私有隔离）。
    doc_block_owner = None if owner_user == PUBLIC_OWNER else owner_user

    rows = conn.execute(
        "SELECT b.block_id, d.title, b.section_path, b.content_desc, b.content, "
        "       b.doc_id, b.entity_id, b.doc_type, b.known_at, b.superseded_at, b.publish_at "
        "  FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.is_leaf AND b.embedding IS NULL "
        "   AND b.owner_user IS NOT DISTINCT FROM %s "
        " ORDER BY b.block_id " + ("LIMIT %s" if limit is not None else ""),
        (doc_block_owner, limit) if limit is not None else (doc_block_owner,),
    ).fetchall()

    if not rows:
        return EmbedStats(pending=0, from_cache=0, computed=0, written=0, indexed=0)

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
    metadata: dict[int, tuple[int, str | None, str, datetime, datetime | None, datetime]] = {
        int(r[0]): (int(r[5]), r[6], str(r[7]), r[8], r[9], r[10]) for r in rows
    }
    keys = {block_id: content_key(text) for block_id, text in inputs.items()}
    key_to_text = {keys[bid]: text for bid, text in inputs.items()}

    cache = EmbeddingCache(conn, model=embedder.model, owner_user=owner_user)
    cached: dict[str, list[float]] = cache.get_many(sorted(set(keys.values())))

    computed = 0
    written = 0
    indexed = 0

    # 按块（而不是按去重后的内容键）分批：这样每一批都能对应到一组具体的
    # block_id，提交才能以"这批块"为粒度发生。缓存 dict 跨批次持续累积，
    # 同样的模板段落只要在更早的批次里算过一次，后面的批次直接命中，
    # 不会重复调用计费 API。
    for start in range(0, len(rows), batch_size):
        chunk_block_ids = [int(r[0]) for r in rows[start : start + batch_size]]

        missing = sorted({keys[bid] for bid in chunk_block_ids if keys[bid] not in cached})
        if missing:
            vectors = [l2_normalize(v) for v in embedder.embed([key_to_text[k] for k in missing])]
            new_vectors = dict(zip(missing, vectors, strict=True))
            cache.put_many(new_vectors)
            cached.update(new_vectors)
            computed += len(missing)

        with conn.transaction():
            for block_id in chunk_block_ids:
                conn.execute(
                    "UPDATE core.doc_block SET embedding = %s WHERE block_id = %s",
                    (_vector_literal(cached[keys[block_id]]), block_id),
                )
                written += 1
        conn.commit()  # 见函数 docstring：上面的 with 块只是 SAVEPOINT。

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

    return EmbedStats(
        pending=len(rows),
        from_cache=len(rows) - computed,
        computed=computed,
        written=written,
        indexed=indexed,
    )
