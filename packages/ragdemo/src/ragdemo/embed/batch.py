"""批量嵌入与断点续传。

待办队列就是 `embedding IS NULL AND is_leaf` —— 不需要额外的状态表，
中断后重跑自然只处理剩余部分（docs/05-document-pipeline.md §5.3）。

P1 首次入库有数十万块，托管 API 有速率限制，所以批处理 + 内容哈希缓存
不是优化，是能否完成 P1 的前提（adr/0004 的后果 1）。
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ragdemo.embed.base import Embedder, content_key, embedding_input
from ragdemo.embed.cache import PUBLIC_OWNER, EmbeddingCache

DEFAULT_BATCH_SIZE = 64


@dataclass(frozen=True)
class EmbedStats:
    pending: int
    from_cache: int
    computed: int
    written: int


def embed_pending_blocks(
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    owner_user: str = PUBLIC_OWNER,
) -> EmbedStats:
    """为尚未嵌入的叶子块生成向量并写回。"""
    rows = conn.execute(
        "SELECT b.block_id, d.title, b.section_path, b.content_desc, b.content "
        "  FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.is_leaf AND b.embedding IS NULL "
        " ORDER BY b.block_id "
        + ("LIMIT %s" if limit is not None else ""),
        (limit,) if limit is not None else (),
    ).fetchall()

    if not rows:
        return EmbedStats(pending=0, from_cache=0, computed=0, written=0)

    inputs: dict[int, str] = {
        int(r[0]): embedding_input(
            doc_title=str(r[1] or ""),
            section_path=str(r[2] or ""),
            content_desc=str(r[3] or ""),
            content=str(r[4]),
        )
        for r in rows
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

    return EmbedStats(
        pending=len(rows),
        from_cache=len(rows) - computed,
        computed=computed,
        written=written,
    )
