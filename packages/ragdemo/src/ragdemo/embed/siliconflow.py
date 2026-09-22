"""硅基流动嵌入适配器（adr/0004）。POST /v1/embeddings。

不接线：`ingest/definitions.py` 与 `evals/cli.py` 的默认装配仍然是
`MockEmbedder`——库里 `core.doc_block.embedding` 全是 Mock 的 sha256 伪
向量，在没有补齐评测集基线之前切换默认嵌入器就是 `CLAUDE.md` §1.4 说的
"改模型"，必须先跑三套评测集。这里只是把真实适配器写出来、跑通契约测试，
供将来评测集就绪时改一个环境变量即可切换。
"""

from __future__ import annotations

from collections.abc import Sequence

from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.http import HttpClient
from ragdemo.embed.base import EMBEDDING_DIM, l2_normalize

ENDPOINT = "/v1/embeddings"
DEFAULT_BATCH_SIZE = 32


class SiliconFlowEmbedder:
    def __init__(
        self,
        http: HttpClient,
        *,
        model: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.model = model
        self._http = http
        self._batch_size = batch_size

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            raise ValueError("嵌入批次不能为空")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        raw = self._http.post_json(
            ENDPOINT,
            {"model": self.model, "input": list(batch), "encoding_format": "float"},
        )
        payload = raw.payload
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(batch):
            got = len(data) if isinstance(data, list) else type(payload).__name__
            raise UpstreamUnavailable(
                f"siliconflow {ENDPOINT} 返回 {got} 条嵌入，期望 {len(batch)} 条"
            )

        # 不信任响应数组的顺序——协议约定用 index 字段对应输入位置，不是
        # 数组下标本身（供应商侧允许乱序返回，实测未必总是原序）。
        ordered = sorted(data, key=_index_of)
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item["embedding"] if isinstance(item, dict) else None
            if not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
                got_dim = len(vector) if isinstance(vector, list) else "?"
                raise ValueError(
                    f"siliconflow 返回 {got_dim} 维向量，期望 {EMBEDDING_DIM} 维"
                    "（ADR-0004 维度固定，悄悄错掉的维度会污染整列）"
                )
            # ADR-0004：写入端与查询端都要归一化，无条件补一次——幂等，
            # 对已经归一化的向量再算一次不改变结果。
            vectors.append(l2_normalize(vector))
        return vectors


def _index_of(item: object) -> int:
    if not isinstance(item, dict) or "index" not in item:
        raise UpstreamUnavailable(f"siliconflow {ENDPOINT} 响应条目缺少 index 字段: {item!r}")
    index = item["index"]
    if not isinstance(index, int):
        raise UpstreamUnavailable(f"siliconflow {ENDPOINT} 响应条目的 index 不是整数: {index!r}")
    return index
