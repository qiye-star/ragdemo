"""切块参数。

长度取 200–400 字，见 docs/05-document-pipeline.md §3.1。
做成配置而非常量，是因为最终值要由 P1c 的检索评测集实测确定。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

# chunk_document/split_text 的切分算法版本。参数指纹（下面 version 属性）
# 只覆盖"用同一套算法、换了参数值"这种变化；算法本身变了（比如换一种句子
# 边界判定方式）要手动把这个常量升到下一版，否则旧算法产出的块会被误判成
# "参数没变、可以复用"。
_ALGORITHM_VERSION = "v1"


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

    @property
    def version(self) -> str:
        """切块参数的指纹，落 `core.doc_block.chunking_version`。

        改 `ChunkConfig` 任意一个字段都会产生不同的值——与 `parse_engine`
        携带 `param_fp`（`05-document-pipeline.md` §2.2）是同一个理由：
        评测集的 gold_block_ids 绑在切块结果上，参数一变旧结果就不能再当
        基准用，需要能明确区分"这批块是用哪套参数切出来的"。
        """
        fingerprint = hashlib.sha256(repr(asdict(self)).encode("utf-8")).hexdigest()[:16]
        return f"chunker:{_ALGORITHM_VERSION}+{fingerprint}"

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
