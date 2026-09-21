"""检索指标（docs/06-retrieval.md §9.1）。

recall@k 是**逐题二值**的：top-k 中包含至少一个 gold 即算命中。
不是 gold 的覆盖比例——同一事实常出现在多处，要求全覆盖会低估系统。
"""
from __future__ import annotations

from collections.abc import Sequence


def _require_gold(gold: set[int]) -> None:
    if not gold:
        raise ValueError("gold_block_ids 为空；评测用例必须至少有一个正确答案")


def recall_at_k(retrieved: Sequence[int], gold: set[int], k: int) -> float:
    _require_gold(gold)
    return 1.0 if set(retrieved[:k]) & gold else 0.0


def mrr_at_k(retrieved: Sequence[int], gold: set[int], k: int) -> float:
    _require_gold(gold)
    for position, block_id in enumerate(retrieved[:k], start=1):
        if block_id in gold:
            return 1.0 / position
    return 0.0
