"""按 gold 块类型 × 出题方式分组的检索指标（表格处理改进计划 T1）。

合成题（auto:）里表格题是看着"描述 + 正文"写出来的，对查描述的改动天然偏高，
所以 table/auto 只作回归护栏，改进证据只认 table/human（计划"评测口径"）。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

MIN_GROUP_SIZE: Final = 10


def case_group(gold_types: Collection[str], *, author: str) -> str:
    kind = "table" if "table" in gold_types else "other"
    origin = "auto" if author.startswith("auto:") else "human"
    return f"{kind}/{origin}"


@dataclass(frozen=True)
class CaseOutcome:
    group: str
    hit10: float
    hit50: float
    rr10: float


@dataclass(frozen=True)
class GroupMetrics:
    n: int
    recall_at_10: float
    recall_at_50: float
    mrr_at_10: float
    gated: bool

    def as_dict(self) -> dict[str, float | int | bool]:
        return {
            "n": self.n,
            "recall_at_10": self.recall_at_10,
            "recall_at_50": self.recall_at_50,
            "mrr_at_10": self.mrr_at_10,
            "gated": self.gated,
        }


def summarize_groups(rows: Sequence[CaseOutcome]) -> dict[str, GroupMetrics]:
    buckets: dict[str, list[CaseOutcome]] = {}
    for row in rows:
        buckets.setdefault(row.group, []).append(row)
    return {
        group: GroupMetrics(
            n=len(items),
            recall_at_10=sum(i.hit10 for i in items) / len(items),
            recall_at_50=sum(i.hit50 for i in items) / len(items),
            mrr_at_10=sum(i.rr10 for i in items) / len(items),
            gated=len(items) >= MIN_GROUP_SIZE,
        )
        for group, items in sorted(buckets.items())
    }
