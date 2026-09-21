"""嵌入协议与工具。

维度固定 1024（adr/0004）：bge-m3 原生 1024，Qwen3-Embedding 经 MRL 降到 1024，
两者可互换而不改表结构。换模型只是数据回填，不是架构变更。
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

EMBEDDING_DIM = 1024
DEFAULT_MAX_EMBED_CHARS = 2000


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """HNSW 索引用 vector_cosine_ops，归一化后余弦距离与内积等价。
    查询端也必须归一化——两端不一致会静默返回错误的排序。"""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def embedding_input(
    *,
    doc_title: str,
    section_path: str,
    content_desc: str,
    content: str,
    max_chars: int = DEFAULT_MAX_EMBED_CHARS,
) -> str:
    """拼接嵌入输入。超长时截断正文而非丢弃前面的上下文——
    「本期」「上述」这类指代在孤立块中无法解析。"""
    parts = [p for p in (doc_title, section_path, content_desc, content) if p]
    return "\n".join(parts)[:max_chars]


def content_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@runtime_checkable
class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """返回与输入等长的向量列表，每个向量 EMBEDDING_DIM 维且已 L2 归一化。"""
