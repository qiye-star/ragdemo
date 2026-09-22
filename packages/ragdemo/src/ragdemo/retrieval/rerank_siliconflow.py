"""硅基流动重排适配器（adr/0004）。POST /v1/rerank。

单独一个模块，不塞进 `rerank.py`：这样 `retrieval/service.py` 的正常导入
路径不会传递性地拖进 `HttpClient`/httpx 依赖——Mock 场景（测试、离线评测）
不需要这条依赖链。

不接线：同 `embed/siliconflow.py` 的裁决 5——默认仍是 `MockReranker`。

重排路径本身一行不改：`rerank_or_degrade()` 已经捕获任何异常并置
`degraded=True`（`rerank.py`），`service.py` 已经区分"关闭"与"尝试过但
失败"两种语义。这个适配器只需要老实实现 `Reranker` Protocol 的契约，
不需要（也不应该）自己再包一层 try/except 去抢那个降级判断。
"""

from __future__ import annotations

from collections.abc import Sequence

from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.http import HttpClient

ENDPOINT = "/v1/rerank"


class SiliconFlowReranker:
    def __init__(self, http: HttpClient, *, model: str) -> None:
        self.model = model
        self._http = http

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        if not docs:
            return []

        raw = self._http.post_json(
            ENDPOINT,
            {
                "model": self.model,
                "query": query,
                "documents": list(docs),
                "top_n": top_k,
                "return_documents": False,
            },
        )
        payload = raw.payload
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise UpstreamUnavailable(f"siliconflow {ENDPOINT} 响应缺少 results 列表")

        n = len(docs)
        ranked: list[tuple[int, float]] = [_parse_result(item, n) for item in results]

        # 协议要求按分数降序、长度 <= top_k——不假设上游已经排好或已经截断。
        ranked.sort(key=lambda pair: -pair[1])
        return ranked[:top_k]


def _parse_result(item: object, doc_count: int) -> tuple[int, float]:
    if not isinstance(item, dict) or "index" not in item or "relevance_score" not in item:
        raise UpstreamUnavailable(
            f"siliconflow {ENDPOINT} 响应条目缺少 index/relevance_score 字段: {item!r}"
        )
    index = item["index"]
    score = item["relevance_score"]
    if not isinstance(index, int) or not (0 <= index < doc_count):
        raise UpstreamUnavailable(
            f"siliconflow {ENDPOINT} 返回越界 index: {index!r}（文档数={doc_count}）"
        )
    if not isinstance(score, int | float):
        raise UpstreamUnavailable(
            f"siliconflow {ENDPOINT} 返回的 relevance_score 不是数字: {score!r}"
        )
    return index, float(score)
