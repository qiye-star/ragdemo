"""确定性 Mock 嵌入器。

从内容哈希派生向量，因此同样的文本永远得到同样的向量——
检索评测在 Mock 上可复现，是 P1 能在没有嵌入 API 配额时推进的前提。
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

from ragdemo.embed.base import EMBEDDING_DIM, l2_normalize


class MockEmbedder:
    model = "mock-1024"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            raise ValueError("嵌入批次不能为空")
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        raw = b""
        counter = 0
        needed = EMBEDDING_DIM * 4
        while len(raw) < needed:
            raw += hashlib.sha256(f"{text}:{counter}".encode()).digest()
            counter += 1
        floats = struct.unpack(f"<{EMBEDDING_DIM}f", raw[:needed])
        cleaned = [0.0 if (x != x or x in (float("inf"), float("-inf"))) else x for x in floats]
        return l2_normalize(cleaned)
