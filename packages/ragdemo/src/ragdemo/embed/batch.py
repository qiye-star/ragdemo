"""批量嵌入与断点续传。

待办队列就是 `embedding IS NULL AND is_leaf` —— 不需要额外的状态表，
中断后重跑自然只处理剩余部分（docs/05-document-pipeline.md §5.3）。

P1 首次入库有数十万块，托管 API 有速率限制，所以批处理 + 内容哈希缓存
不是优化，是能否完成 P1 的前提（adr/0004 的后果 1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from ragdemo.embed.base import Embedder, content_key, embedding_input
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

    `index` 非 None 时，在 PG 事务提交后把同一批块写入向量索引。顺序很重要：
    **先 PG 提交，后写 Chroma**。反过来会在 PG 回滚时留下 Chroma 里的孤儿
    向量，而孤儿向量正是 ADR-0009 要防的泄漏源——它们不受 PG 事务保护，
    没有对应的 core.doc_block 行撤销它们。写向量索引失败不回滚 PG：PG 是
    真相，向量索引是可重建的派生物，重跑本函数会补上。
    """
    rows = conn.execute(
        "SELECT b.block_id, d.title, b.section_path, b.content_desc, b.content, "
        "       b.doc_id, b.entity_id, b.doc_type, b.known_at, b.superseded_at, b.publish_at "
        "  FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.is_leaf AND b.embedding IS NULL "
        " ORDER BY b.block_id " + ("LIMIT %s" if limit is not None else ""),
        (limit,) if limit is not None else (),
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

    cache = EmbeddingCache(conn, model=embedder.model, owner_user=owner_user)
    cached = cache.get_many(sorted(set(keys.values())))

    missing_keys = sorted({k for k in keys.values() if k not in cached})
    key_to_text = {keys[bid]: text for bid, text in inputs.items()}

    computed = 0
    for start in range(0, len(missing_keys), batch_size):
        chunk = missing_keys[start : start + batch_size]
        vectors = embedder.embed([key_to_text[k] for k in chunk])
        cache.put_many(dict(zip(chunk, vectors, strict=True)))
        cached.update(dict(zip(chunk, vectors, strict=True)))
        computed += len(chunk)

    written = 0
    with conn.transaction():
        for block_id, key in keys.items():
            conn.execute(
                "UPDATE core.doc_block SET embedding = %s WHERE block_id = %s",
                ("[" + ",".join(repr(float(x)) for x in cached[key]) + "]", block_id),
            )
            written += 1

    # 先 PG 提交（上面的 with 块已经做了），后写向量索引：反过来会在 PG
    # 回滚时留下 Chroma 里的孤儿向量。写向量索引失败不回滚 PG。
    indexed = 0
    if index is not None:
        items = [
            VectorItem(
                block_id=block_id,
                embedding=cached[keys[block_id]],
                doc_id=meta[0],
                entity_id=meta[1],
                doc_type=meta[2],
                known_at=meta[3],
                superseded_at=meta[4],
                publish_at=meta[5],
            )
            for block_id, meta in metadata.items()
        ]
        index.upsert(items)
        indexed = len(items)

    return EmbedStats(
        pending=len(rows),
        from_cache=len(rows) - computed,
        computed=computed,
        written=written,
        indexed=indexed,
    )
