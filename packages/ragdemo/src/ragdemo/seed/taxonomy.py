"""产业链环节表。

`l1_layer` / `l2_segment` / `l3_node` 在数据库里都是无约束的自由文本，
却是 `entity`、`entity_node_membership`、`node_metric`、`propagation_rule`、
`opinion` 五张表之间的事实联结键（docs/08-evaluation.md §3 的基准构造与
docs/07-agents.md 的规则匹配都是精确字符串相等）。

后果是：一个错字不会报任何错，只会让该环节的评分基准悄悄变成空集、
让传导规则悄悄匹配不到任何实体。本文件把环节表变成唯一事实来源，
导入时交叉校验，把「静默错」换成「导入失败」。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TAXONOMY_PATH = Path("db/seed/taxonomy.csv")

_REQUIRED_COLUMNS = ("l1_layer", "l2_segment", "l3_node")


@dataclass(frozen=True)
class Taxonomy:
    """在册的全部 L3 环节，以及每个环节的上层归属。"""

    by_node: dict[str, tuple[str, str]]

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self.by_node)

    def unknown(self, candidates: list[str]) -> list[str]:
        """返回不在册的环节名，保持传入顺序、去重。"""
        seen: list[str] = []
        for c in candidates:
            if c and c not in self.by_node and c not in seen:
                seen.append(c)
        return seen


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> Taxonomy:
    """读环节表。同名环节出现两次即报错——挂在两个 L2 下会让加权与基准二义。"""
    # 延迟导入：taxonomy 是 loader 的下层，正向 import 会成环。
    from ragdemo.seed.loader import SeedError

    if not path.exists():
        raise SeedError(f"环节表不存在: {path}（它是全部环节名的唯一事实来源）")

    by_node: dict[str, tuple[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = set(_REQUIRED_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise SeedError(f"{path.name} 缺少列: {sorted(missing)}")
        for i, row in enumerate(reader, start=2):
            node = (row["l3_node"] or "").strip()
            if not node:
                raise SeedError(f"{path.name} 第 {i} 行 l3_node 为空")
            if node in by_node:
                raise SeedError(f"{path.name} 第 {i} 行 l3_node 重复: {node}")
            by_node[node] = (
                (row["l1_layer"] or "").strip(),
                (row["l2_segment"] or "").strip(),
            )
    return Taxonomy(by_node=by_node)
