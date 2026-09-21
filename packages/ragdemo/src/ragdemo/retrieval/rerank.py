"""重排与降级。

重排输入用**叶子块**的 content，不用父块——父块太长会超出重排模型上下文，
且稀释相关信号（docs/06-retrieval.md §5）。

降级发生率要监控：持续降级说明需要把重排模型本地化（adr/0004 的推翻条件）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ragdemo.retrieval.fusion import FusedHit

logger = logging.getLogger(__name__)


@runtime_checkable
class Reranker(Protocol):
    model: str

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        """返回 (原索引, 相关度分)，按分数降序，长度 ≤ top_k。"""


@dataclass(frozen=True)
class RerankOutcome:
    order: list[int]
    scores: dict[int, float]
    degraded: bool


class MockReranker:
    """按字符重叠打分。确定性，供测试与离线开发。"""

    model = "mock-reranker"

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        query_chars = set(query)
        scored = [
            (i, len(query_chars & set(doc)) / max(len(query_chars), 1))
            for i, doc in enumerate(docs)
        ]
        scored.sort(key=lambda p: (-p[1], p[0]))
        return scored[:top_k]


class FailingReranker:
    """总是抛异常。用于验证降级路径。"""

    model = "failing"

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        raise RuntimeError("重排服务不可用")


def rerank_or_degrade(
    reranker: Reranker,
    query: str,
    fused: Sequence[FusedHit],
    contents: Mapping[int, str],
    top_k: int,
) -> RerankOutcome:
    """重排失败时回退到 RRF 顺序，并标记 degraded。"""
    if not fused:
        return RerankOutcome(order=[], scores={}, degraded=False)

    block_ids = [f.block_id for f in fused]
    docs = [contents.get(b, "") for b in block_ids]

    try:
        ranked = reranker.rerank(query, docs, top_k)
    except Exception:  # 任何失败都必须降级，不能让查询挂掉（本仓库未启用 flake8-blind-except）
        logger.warning("rerank_degraded", extra={"model": reranker.model, "n": len(fused)})
        head = list(fused)[:top_k]
        return RerankOutcome(
            order=[f.block_id for f in head],
            scores={f.block_id: f.score for f in head},
            degraded=True,
        )

    return RerankOutcome(
        order=[block_ids[i] for i, _ in ranked],
        scores={block_ids[i]: score for i, score in ranked},
        degraded=False,
    )
