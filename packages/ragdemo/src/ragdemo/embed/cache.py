"""内容哈希缓存。

各家公告中大量重复的模板段落只需算一次，对 5000 元/月的预算是实质节省。
主键三列缺一不可，理由见 docs/05-document-pipeline.md §5.3。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import psycopg

from ragdemo.embed.base import EMBEDDING_DIM, l2_normalize

PUBLIC_OWNER = ""


class EmbeddingCache:
    def __init__(
        self, conn: psycopg.Connection, *, model: str, owner_user: str = PUBLIC_OWNER
    ) -> None:
        self.conn = conn
        self.model = model
        self.owner_user = owner_user

    def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        rows = self.conn.execute(
            "SELECT content_hash, embedding FROM core.embedding_cache "
            " WHERE model = %s AND owner_user = %s AND content_hash = ANY(%s)",
            (self.model, self.owner_user, list(keys)),
        ).fetchall()
        return {str(r[0]): _to_floats(r[1]) for r in rows}

    def put_many(self, items: Mapping[str, Sequence[float]]) -> int:
        written = 0
        with self.conn.transaction():
            for key, vector in items.items():
                if len(vector) != EMBEDDING_DIM:
                    raise ValueError(f"向量维度应为 {EMBEDDING_DIM}，收到 {len(vector)}")
                # 写库前强制归一化，不信任调用方已经做过（base.Embedder 协议
                # 只在 docstring 里要求，没有运行时保证）。HNSW 索引用
                # vector_cosine_ops，一个没归一化的向量混进去不会报错，只会
                # 静默把排序算错（docs/adr/0004）。l2_normalize 幂等，对已经
                # 归一化的向量再算一次不改变结果。
                self.conn.execute(
                    "INSERT INTO core.embedding_cache (content_hash, model, owner_user,"
                    " embedding) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (content_hash, model, owner_user) DO NOTHING",
                    (key, self.model, self.owner_user, _to_literal(l2_normalize(vector))),
                )
                written += 1
        return written


def _to_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _to_floats(value: object) -> list[float]:
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",")]
    # psycopg 的 pgvector 适配器把 vector 列读成一个可迭代对象（通常是
    # numpy 数组或 pgvector 自带的 Vector 类型），但类型标注只声明为
    # object——这里没有为它加类型标注依赖，用 attr-defined 精确抑制。
    return [float(x) for x in value]  # type: ignore[attr-defined]
