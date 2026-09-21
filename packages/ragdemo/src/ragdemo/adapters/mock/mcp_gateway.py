"""MCP 网关的 Mock：不发网络，工具清单与返回值都来自内存。

存在的理由不是「方便」，是 CLAUDE.md §3 的硬要求——每个外部适配器都要有 Mock 与
契约测试。具体到这个网关，它还解决三个真实问题：

1. **别花钱**。网关背后的 Wind / iFind / Tushare 是付费额度，iFind 还有套餐并发
   硬限（免费 2 / 个人 5 / 企业 10）。CI 里跑真实调用等于每次提交都在烧额度。
2. **别把测试绑在外部可用性上**。网关是独立部署的进程，它下线不该让本仓库的
   测试套变红。
3. **能构造真实网关难以复现的分支**——业务失败（isError）、协议畸形帧、
   工具不存在。这些在真实网关上要么造不出来，要么造出来代价很高。

工具清单默认取真实网关的形状（Tushare 原生名 + `wind_*` / `ifind_*` 元工具），
这样契约测试覆盖的是真实的命名约定，不是一个理想化的假设。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.mcp_gateway import (
    MCP_ENDPOINT,
    PROVIDER,
    McpToolFailed,
    ToolCall,
    ToolSpec,
)

# 形状照搬真实网关：Tushare 用原生名，Wind/iFind 是带前缀的 3 个元工具
# （懒发现，不摊开底下 35/32 个具体接口），免费源各自原生名。
DEFAULT_TOOLS: Sequence[ToolSpec] = (
    ToolSpec(
        "daily", "行情数据", {"type": "object", "properties": {"ts_code": {"type": "string"}}}
    ),
    ToolSpec("income", "利润表", {"type": "object", "properties": {"ts_code": {"type": "string"}}}),
    ToolSpec("wind_list_apis", "列出万得接口", {"type": "object"}),
    ToolSpec("wind_get_api_info", "查看万得接口说明", {"type": "object"}),
    ToolSpec("wind_query", "调用万得接口", {"type": "object"}),
    ToolSpec("ifind_list_apis", "列出同花顺接口", {"type": "object"}),
    ToolSpec("ifind_get_api_info", "查看同花顺接口说明", {"type": "object"}),
    ToolSpec("ifind_query", "调用同花顺接口", {"type": "object"}),
    ToolSpec("get_market_headlines", "市场头条", {"type": "object"}),
)


class MockMcpGateway:
    """与 `McpGatewayClient` 同形的内存实现。

    `responses` 按工具名给定返回值：值是 str 时原样当文本返回，其他类型按 JSON
    序列化——真实网关的 `content[0].text` 永远是字符串，工具自己决定里面是不是 JSON。
    """

    provider = PROVIDER

    def __init__(
        self,
        *,
        tools: Sequence[ToolSpec] | None = None,
        responses: Mapping[str, Any] | None = None,
        failing: Mapping[str, str] | None = None,
        healthy: bool = True,
    ) -> None:
        self._tools = list(tools if tools is not None else DEFAULT_TOOLS)
        self._responses = dict(responses or {})
        # 业务失败（积分不足、无权限）——与传输失败分开，重试策略相反。
        self._failing = dict(failing or {})
        self._healthy = healthy
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def health(self) -> bool:
        return self._healthy

    def list_tools(self) -> list[ToolSpec]:
        if not self._healthy:
            raise UpstreamUnavailable(f"{PROVIDER} 不可用（Mock）")
        return list(self._tools)

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolCall:
        self.calls.append((name, dict(arguments)))

        if not self._healthy:
            raise UpstreamUnavailable(f"{PROVIDER} 不可用（Mock）")

        known = {t.name for t in self._tools}
        if name not in known:
            # 真实网关的路由是「工具名 → 唯一源」，名字不在表里就是没有这个工具，
            # 不存在「换个源再试」——跨源取舍是调用方的事，不是网关的路由行为。
            raise UpstreamUnavailable(f"{PROVIDER} 没有名为 {name} 的工具")

        if name in self._failing:
            raise McpToolFailed(f"{PROVIDER} 工具 {name} 业务失败：{self._failing[name]}")

        value = self._responses.get(name, {"code": 0, "data": []})
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            payload = None

        # 快照要和真实网关同形（SSE 帧），否则照 Mock 写的解析代码搬过去会失效。
        frame = json.dumps(
            {"result": {"content": [{"type": "text", "text": text}]}}, ensure_ascii=False
        )
        return ToolCall(
            tool=name,
            text=text,
            payload=payload,
            is_error=False,
            raw=RawResponse(
                provider=PROVIDER,
                endpoint=MCP_ENDPOINT,
                params={"name": name, "arguments": dict(arguments)},
                payload=f"event: message\ndata: {frame}\n",
                http_status=200,
                fetched_at=datetime.now(UTC),
                cost_cents=Decimal(0),
            ),
        )


__all__ = ("DEFAULT_TOOLS", "MockMcpGateway")
