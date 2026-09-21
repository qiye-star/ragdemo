"""环节表：五张表靠 L3 环节名精确相等联结，数据库一个字都不校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragdemo.seed.loader import SeedError
from ragdemo.seed.taxonomy import DEFAULT_TAXONOMY_PATH, load_taxonomy

ANCHOR_NODES = {"云端训练芯片", "边缘推理芯片", "先进封装"}


def test_repo_taxonomy_contains_the_nodes_the_docs_already_name() -> None:
    """文档里已经出现过的环节名必须在册，否则文档与数据对不上。"""
    taxonomy = load_taxonomy(DEFAULT_TAXONOMY_PATH)
    assert taxonomy.nodes >= ANCHOR_NODES


def test_repo_taxonomy_anchors_l1_layers() -> None:
    taxonomy = load_taxonomy(DEFAULT_TAXONOMY_PATH)
    layers = {l1 for l1, _ in taxonomy.by_node.values()}
    assert layers >= {"算力", "模型", "应用", "数据", "能源"}


def test_duplicate_node_is_rejected(tmp_path: Path) -> None:
    """同名环节挂在两个 L2 下，加权与基准就会二义。"""
    p = tmp_path / "taxonomy.csv"
    p.write_text(
        "l1_layer,l2_segment,l3_node,description\n"
        "算力,AI芯片,云端训练芯片,训练卡\n"
        "算力,存储,云端训练芯片,写重了\n",
        encoding="utf-8",
    )
    with pytest.raises(SeedError, match="重复"):
        load_taxonomy(p)


def test_lookup_returns_parent_layers(tmp_path: Path) -> None:
    p = tmp_path / "taxonomy.csv"
    p.write_text(
        "l1_layer,l2_segment,l3_node,description\n算力,AI芯片,云端训练芯片,训练卡\n",
        encoding="utf-8",
    )
    taxonomy = load_taxonomy(p)
    assert taxonomy.by_node["云端训练芯片"] == ("算力", "AI芯片")
