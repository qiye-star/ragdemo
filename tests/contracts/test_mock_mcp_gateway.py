"""Mock MCP 网关的契约测试，外加 Mock 特有的分支。"""

from __future__ import annotations

import pytest

from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.mcp_gateway import McpToolFailed
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from tests.contracts.mcp_gateway_contract import McpGatewayContract, _GatewayLike


class TestMockMcpGateway(McpGatewayContract):
    def make_client(self) -> _GatewayLike:
        return MockMcpGateway(responses={"daily": {"code": 0, "data": [[1, 2]]}})

    def working_tool(self) -> str:
        return "daily"


def test_default_tool_shape_matches_the_real_gateway() -> None:
    """Wind/iFind 在真实网关上是 3 个元工具（懒发现），不是摊开的 35/32 个接口。

    Mock 若摊开成具体接口，用它写的调用代码搬到真实网关上会全部找不到工具。
    """
    names = {t.name for t in MockMcpGateway().list_tools()}

    assert {"wind_list_apis", "wind_get_api_info", "wind_query"} <= names
    assert {"ifind_list_apis", "ifind_get_api_info", "ifind_query"} <= names
    assert not any(n.startswith("wind_") and n.endswith("_raw") for n in names)


def test_business_failure_is_not_a_transport_failure() -> None:
    """积分不足重试多少次都一样；传输抖动才值得重试。两者必须能分开。"""
    gw = MockMcpGateway(failing={"income": "积分不足，需要更高权限"})

    with pytest.raises(McpToolFailed, match="积分不足"):
        gw.call_tool("income", {})


def test_unhealthy_gateway_raises_upstream_unavailable() -> None:
    gw = MockMcpGateway(healthy=False)

    assert gw.health() is False
    with pytest.raises(UpstreamUnavailable):
        gw.list_tools()


def test_calls_are_recorded_for_assertions() -> None:
    gw = MockMcpGateway()
    gw.call_tool("daily", {"ts_code": "688256.SH"})

    assert gw.calls == [("daily", {"ts_code": "688256.SH"})]


def test_string_response_is_kept_as_text_not_reserialised() -> None:
    """真实网关的 content[0].text 永远是字符串，工具自己决定里面是不是 JSON。"""
    gw = MockMcpGateway(responses={"daily": "not json at all"})
    result = gw.call_tool("daily", {})

    assert result.text == "not json at all"
    assert result.payload is None
