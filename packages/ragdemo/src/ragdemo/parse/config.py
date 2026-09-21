"""切块参数。

长度取 200–400 字，见 docs/05-document-pipeline.md §3.1。
做成配置而非常量，是因为最终值要由 P1c 的检索评测集实测确定。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkConfig:
    leaf_min_chars: int = 200
    leaf_max_chars: int = 400
    overlap_ratio: float = 0.12
    keep_table_whole: bool = True
    drop_boilerplate: bool = True

    @property
    def overlap_chars(self) -> int:
        return int(self.leaf_max_chars * self.overlap_ratio)

    def validate(self) -> None:
        if not 0.0 <= self.overlap_ratio < 1.0:
            raise ValueError(f"overlap_ratio 必须在 [0, 1) 内，收到 {self.overlap_ratio}")
        if self.leaf_min_chars > self.leaf_max_chars:
            raise ValueError(
                f"leaf_min_chars {self.leaf_min_chars} 大于 leaf_max_chars {self.leaf_max_chars}"
            )
        if self.overlap_chars >= self.leaf_min_chars:
            raise ValueError(
                f"overlap {self.overlap_chars} 不小于 leaf_min_chars {self.leaf_min_chars}，"
                "切分不会收敛"
            )
