"""检索的请求、配置与结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RetrievalConfig:
    candidate_k: int = 50
    fusion_k: int = 50
    top_k: int = 10
    w_bm25: float = 0.6
    w_vec: float = 0.4
    rrf_k: int = 60
    ef_search: int = 200
    max_scan_tuples: int = 200_000
    # 向量一路过滤后不足 candidate_k 时的过采样倍数增长率。ADR-0009 后果 3
    # 把它定性为评测参数——改动要跑评测并把数字贴进 PR，所以它必须在这里
    # 可配置，而不是藏在 vector.py 的模块常量里。
    oversample_growth: int = 4
    rewrite_enabled: bool = False
    rerank_enabled: bool = True

    def validate(self) -> None:
        if self.top_k > self.fusion_k:
            raise ValueError(f"top_k {self.top_k} 不能大于 fusion_k {self.fusion_k}")
        if self.fusion_k > self.candidate_k * 2:
            raise ValueError("fusion_k 超出两路候选之和的上限")
        if self.w_bm25 <= 0 and self.w_vec <= 0:
            raise ValueError("两路权重不能同时为 0")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k 必须为正")
        if self.oversample_growth < 2:
            # 等于 1 会让迭代过采样原地踏步（k 永远不变），直接死循环。
            raise ValueError("oversample_growth 必须至少为 2，否则过采样不会增长")


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    as_of: datetime
    entity_ids: list[str] | None = None
    doc_types: list[str] | None = None
    published_after: datetime | None = None
    tenant: str | None = None
    user: str | None = None
    config: RetrievalConfig = field(default_factory=RetrievalConfig)

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query 不能为空")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError(f"as_of 必须带时区，收到 {self.as_of!r}")
        self.config.validate()


@dataclass(frozen=True)
class EvidenceBlock:
    block_id: int
    parent_block_id: int | None
    content: str
    matched_child_ids: list[int]
    doc_id: int
    doc_title: str
    section_path: str
    page: int | None
    known_at: datetime
    score: float
    reranked: bool


@dataclass(frozen=True)
class RetrievalStats:
    bm25_hits: int = 0
    vec_hits: int = 0
    after_fusion: int = 0
    after_rerank: int = 0
    ms_bm25: float = 0.0
    ms_vec: float = 0.0
    ms_rerank: float = 0.0
    ms_total: float = 0.0
    degraded: bool = False


@dataclass(frozen=True)
class RetrievalResult:
    blocks: list[EvidenceBlock]
    stats: RetrievalStats
