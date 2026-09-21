"""切块配置。默认值来自本计划开头的裁定，且必须自洽。"""

from __future__ import annotations

import pytest

from ragdemo.parse.config import ChunkConfig


def test_defaults_match_the_ruling() -> None:
    cfg = ChunkConfig()
    assert (cfg.leaf_min_chars, cfg.leaf_max_chars) == (200, 400)
    assert cfg.overlap_ratio == 0.12
    assert cfg.keep_table_whole is True


def test_overlap_chars_is_derived_from_max() -> None:
    assert ChunkConfig(leaf_max_chars=400, overlap_ratio=0.12).overlap_chars == 48


def test_min_greater_than_max_is_rejected() -> None:
    with pytest.raises(ValueError, match="leaf_min_chars"):
        ChunkConfig(leaf_min_chars=500, leaf_max_chars=400).validate()


def test_overlap_must_be_smaller_than_min_chunk() -> None:
    """重叠 ≥ 最小块长会让切分不收敛——每一步前进的距离为零或负。"""
    with pytest.raises(ValueError, match="overlap"):
        ChunkConfig(leaf_min_chars=200, leaf_max_chars=400, overlap_ratio=0.9).validate()


def test_overlap_ratio_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlap_ratio"):
        ChunkConfig(overlap_ratio=1.5).validate()
