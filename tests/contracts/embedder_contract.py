"""任何 Embedder 实现都必须通过的契约。

用法：子类化 `EmbedderContract`，实现 `make_embedder()`，即自动获得这几条
测试。真实适配器（SiliconFlowEmbedder）与 Mock 走同一套契约——这是「建
适配器但不接线」（裁决 5）能落地的前提：两者行为等价，将来切换只是换一个
构造点，不是改一套新逻辑。

不继承 `tests/contracts/adapter_contract.py::AdapterContract`：那一套测的
是 `provider`/`health`/`fetch`/`known_at`，`Embedder` 一个都没有——硬凑
共同基类只会产生一堆 `NotImplementedError` 桩子。
"""

from __future__ import annotations

import math
from typing import Protocol

import pytest

from ragdemo.embed.base import EMBEDDING_DIM


class _EmbedderLike(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedderContract:
    """契约测试基类。不要在这里加 pytest fixture，子类可能有自己的。"""

    def make_embedder(self) -> _EmbedderLike:
        raise NotImplementedError

    def test_model_is_a_nonempty_string(self) -> None:
        model = self.make_embedder().model
        assert isinstance(model, str) and model

    def test_output_length_matches_input(self) -> None:
        vectors = self.make_embedder().embed(["第一句", "第二句", "第三句"])
        assert len(vectors) == 3

    def test_every_vector_is_embedding_dim(self) -> None:
        vectors = self.make_embedder().embed(["一段文本"])
        assert all(len(v) == EMBEDDING_DIM for v in vectors)

    def test_vectors_are_l2_normalized(self) -> None:
        """ADR-0004：写入端与查询端都要归一化，两端不一致会静默返回错误的排序。"""
        vectors = self.make_embedder().embed(["一段文本", "另一段文本"])
        for v in vectors:
            norm = math.sqrt(sum(x * x for x in v))
            assert abs(norm - 1.0) < 1e-6

    def test_empty_batch_raises(self) -> None:
        with pytest.raises(ValueError):
            self.make_embedder().embed([])
