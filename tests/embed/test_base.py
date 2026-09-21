"""嵌入基础：维度、L2 归一化、输入拼接。"""
from __future__ import annotations

import math

import pytest

from ragdemo.embed.base import EMBEDDING_DIM, embedding_input, l2_normalize
from ragdemo.embed.mock import MockEmbedder


def test_dimension_is_1024() -> None:
    """adr/0004：固定 1024 让 bge-m3 与 Qwen3-Embedding 可互换。"""
    assert EMBEDDING_DIM == 1024


def test_mock_embedder_returns_correct_shape() -> None:
    vectors = MockEmbedder().embed(["甲", "乙"])
    assert len(vectors) == 2
    assert all(len(v) == EMBEDDING_DIM for v in vectors)


def test_mock_embedder_is_deterministic() -> None:
    assert MockEmbedder().embed(["甲"]) == MockEmbedder().embed(["甲"])


def test_different_text_gives_different_vector() -> None:
    a, b = MockEmbedder().embed(["甲", "乙"])
    assert a != b


def test_vectors_are_l2_normalised() -> None:
    """HNSW 用 vector_cosine_ops，两端都必须归一化，否则排序静默出错。"""
    for vector in MockEmbedder().embed(["甲", "乙丙丁"]):
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-6)


def test_l2_normalize_handles_zero_vector() -> None:
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_embedding_input_includes_title_and_section() -> None:
    """标题与章节路径提供块本身缺失的上下文（「本期」「上述」无法孤立解析）。"""
    text = embedding_input(
        doc_title="三季报", section_path="第三节 > 分部收入",
        content_desc="分部收入表", content="智能计算 12,340 万元",
    )
    assert "三季报" in text and "第三节" in text and "分部收入表" in text


def test_embedding_input_truncates_content_not_context() -> None:
    """超长时截断正文，保留前面的标题与章节——上下文比尾部正文更值钱。"""
    text = embedding_input(
        doc_title="三季报", section_path="第三节", content_desc="",
        content="甲" * 5000, max_chars=100,
    )
    assert text.startswith("三季报")
    assert len(text) == 100


def test_embedding_input_skips_empty_parts() -> None:
    text = embedding_input(doc_title="三季报", section_path="", content_desc="", content="甲")
    assert text == "三季报\n甲"


def test_embedder_rejects_empty_batch() -> None:
    with pytest.raises(ValueError, match="空"):
        MockEmbedder().embed([])
