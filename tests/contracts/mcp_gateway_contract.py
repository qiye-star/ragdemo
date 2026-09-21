"""任何 MCP 网关客户端实现都必须通过的契约。

用法：子类化 `McpGatewayContract`，实现 `make_client()`，即自动获得这几条测试。
真实客户端与 Mock 走同一套契约——这是 Mock 能在 CI 里替代真实网关的前提
（真实网关背后是付费额度，每次提交都真调等于烧钱）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import pytest

from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import ToolCall, ToolSpec


class _GatewayLike(Protocol):
    provider: str

    def health(self) -> bool: ...
    def list_tools(self) -> list[ToolSpec]: ...
    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolCall: ...


class McpGatewayContract:
    """契约测试基类。不要在这里加 pytest fixture，子类可能有自己的。"""

    def make_client(self) -> _GatewayLike:
        raise NotImplementedError

    def working_tool(self) -> str:
        """一个在该实现里调得通的工具名。"""
        raise NotImplementedError

    def test_provider_is_a_nonempty_string(self) -> None:
        assert self.make_client().provider == "mcp-gateway"

    def test_health_returns_bool(self) -> None:
        assert isinstance(self.make_client().health(), bool)

    def test_list_tools_returns_specs_with_names(self) -> None:
        specs = self.make_client().list_tools()
        assert specs, "工具清单不应为空"
        assert all(isinstance(s.name, str) and s.name for s in specs)
        assert all(isinstance(s.input_schema, dict) for s in specs)

    def test_tool_names_are_unique(self) -> None:
        """网关的路由是「工具名 → 唯一源」，重名会让路由不确定。"""
        names = [s.name for s in self.make_client().list_tools()]
        assert len(names) == len(set(names))

    def test_call_tool_returns_snapshot_for_traceability(self) -> None:
        """CLAUDE.md §1.5：原始响应与结构化结果都要留。"""
        result = self.make_client().call_tool(self.working_tool(), {})

        assert result.tool == self.working_tool()
        assert result.is_error is False
        assert result.raw.provider == "mcp-gateway"
        assert result.raw.fetched_at.tzinfo is not None, "fetched_at 必须带时区"

    def test_unknown_tool_raises_adapter_error(self) -> None:
        """不存在的工具要抛本层的错误类型，不能漏一个 KeyError 给调用方。"""
        with pytest.raises(AdapterError):
            self.make_client().call_tool("__definitely_not_a_tool__", {})
