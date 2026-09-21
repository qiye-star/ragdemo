"""MCP 网关客户端：帧解析、错误分类、快照留存与不安全端点拦截。

这里的每条断言都对应一个在真实网关（a.finovadeep.com:8766，mcp-gateway 1.30.0）
上实测出来的行为，不是照协议文档猜的：

- 成功是 SSE 帧（`event: message` + 一行 `data:`），失败可能是**裸 JSON 不带帧**；
- 请求体不合法时网关回 HTTP 500 + JSON-RPC error（实测：编码损坏的请求体 0.17s
  就被拒，快到没走上游），而这个响应**不带 SSE 帧**；
- `/mcp` 不带尾斜杠会被 307 重定向，POST 体在重定向后可能丢。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import (
    McpGatewayClient,
    McpProtocolError,
    McpToolFailed,
    gateway_client_from_env,
)


def _sse(obj: dict[str, object]) -> str:
    return f"event: message\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _client(handler: httpx.MockTransport) -> McpGatewayClient:
    http = HttpClient(
        provider="mcp-gateway",
        base_url="http://gateway.test",
        policy=RetryPolicy(max_attempts=2, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=handler,
    )
    return McpGatewayClient(http)


# --- 帧解析 -----------------------------------------------------------------


def test_list_tools_parses_the_sse_frame() -> None:
    body = _sse(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "daily", "description": "行情", "inputSchema": {"type": "object"}},
                    {"name": "wind_query", "description": "万得", "inputSchema": {}},
                ]
            },
        }
    )
    specs = _client(httpx.MockTransport(lambda r: httpx.Response(200, text=body))).list_tools()

    assert [s.name for s in specs] == ["daily", "wind_query"]
    assert specs[0].input_schema == {"type": "object"}


def test_call_tool_returns_the_text_content() -> None:
    body = _sse(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": '{"code":0,"data":[1,2]}'}]},
        }
    )
    result = _client(httpx.MockTransport(lambda r: httpx.Response(200, text=body))).call_tool(
        "daily", {"ts_code": "600519.SH"}
    )

    assert result.tool == "daily"
    assert result.payload == {"code": 0, "data": [1, 2]}
    assert result.is_error is False


def test_non_json_tool_text_is_kept_as_text() -> None:
    """工具返回的不一定是 JSON；保留原文而不是当解析失败处理。"""
    body = _sse(
        {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "ok"}]}}
    )
    result = _client(httpx.MockTransport(lambda r: httpx.Response(200, text=body))).call_tool(
        "ping", {}
    )

    assert result.text == "ok"
    assert result.payload is None


# --- 错误分类 ---------------------------------------------------------------


def test_bare_jsonrpc_error_on_500_is_upstream_unavailable() -> None:
    """实测：请求体编码损坏时网关回这个，且**不带 SSE 帧**。

    照着「成功是 SSE」写死解析的客户端会在这里抛 StopIteration 或 JSONDecodeError，
    把一句可读的错误信息变成看不懂的崩溃。

    这条的来历值得记一笔：最初我以为 search_stock 这个工具坏了（裸 curl 4/4 全 500），
    实际是 Git Bash 把命令行里的中文压成了 GBK，请求体不是合法 UTF-8。同样的调用
    经 httpx 发出去一切正常。0.17s 的耗时是线索——真去查上游不可能这么快。
    """
    body = json.dumps(
        {"jsonrpc": "2.0", "id": "server-error", "error": {"code": -32603, "message": "boom"}}
    )
    client = _client(httpx.MockTransport(lambda r: httpx.Response(500, text=body)))

    with pytest.raises(UpstreamUnavailable, match="boom"):
        client.call_tool("search_stock", {"keyword": "寒武纪"})


def test_is_error_result_raises_tool_failed() -> None:
    """业务失败（isError=True）与传输失败要分开：前者重试无益，后者可能是抖动。"""
    body = _sse(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "积分不足"}], "isError": True},
        }
    )
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, text=body)))

    with pytest.raises(McpToolFailed, match="积分不足"):
        client.call_tool("income", {})


def test_unparseable_body_raises_protocol_error() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, text="<html>nope</html>")))

    with pytest.raises(McpProtocolError):
        client.list_tools()


# --- 传输细节 ---------------------------------------------------------------


def test_endpoint_keeps_the_trailing_slash() -> None:
    """不带尾斜杠会被 307 重定向，POST 体在跳转后可能丢——网关 README 明写的坑。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text=_sse({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))

    _client(httpx.MockTransport(handler)).list_tools()

    assert seen == ["/mcp/"]


def test_accept_header_allows_event_stream() -> None:
    """少了 text/event-stream，服务端不会用 SSE 应答。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept", ""))
        return httpx.Response(200, text=_sse({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))

    _client(httpx.MockTransport(handler)).list_tools()

    assert "text/event-stream" in seen[0]


def test_raw_response_is_captured_for_provider_snapshot() -> None:
    """CLAUDE.md §1.5：入库时同时保留原始响应与结构化结果。"""
    body = _sse({"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "x"}]}})
    result = _client(httpx.MockTransport(lambda r: httpx.Response(200, text=body))).call_tool(
        "daily", {}
    )

    assert result.raw.provider == "mcp-gateway"
    assert result.raw.http_status == 200
    assert result.raw.fetched_at.tzinfo is not None
    assert "event: message" in str(result.raw.payload)


# --- 出网闸门 ---------------------------------------------------------------


def test_env_without_url_refuses_to_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有默认地址：把一个明文公网端点写死进仓库是本仓库明确禁止的。"""
    monkeypatch.delenv("RAGDEMO_MCP_GATEWAY_URL", raising=False)

    with pytest.raises(ValueError, match="RAGDEMO_MCP_GATEWAY_URL"):
        gateway_client_from_env()


def test_env_rejects_plaintext_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """明文 HTTP 发往公网 = 实体财务数据在链路上裸奔（CLAUDE.md §0）。"""
    monkeypatch.setenv("RAGDEMO_MCP_GATEWAY_URL", "http://a.example.com:8766")
    monkeypatch.delenv("RAGDEMO_MCP_GATEWAY_ALLOW_INSECURE", raising=False)

    with pytest.raises(ValueError, match="明文"):
        gateway_client_from_env()


def test_env_allows_plaintext_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机/内网走明文没有链路暴露问题，不该被拦。"""
    monkeypatch.setenv("RAGDEMO_MCP_GATEWAY_URL", "http://127.0.0.1:8766")

    assert gateway_client_from_env().provider == "mcp-gateway"


def test_env_allows_https_to_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGDEMO_MCP_GATEWAY_URL", "https://a.example.com:8766")

    assert gateway_client_from_env().provider == "mcp-gateway"


def test_insecure_override_is_explicit_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """允许显式放行，但必须留下一条 WARNING——静默降级是安全问题里最常见的开头。"""
    monkeypatch.setenv("RAGDEMO_MCP_GATEWAY_URL", "http://a.example.com:8766")
    monkeypatch.setenv("RAGDEMO_MCP_GATEWAY_ALLOW_INSECURE", "1")

    with caplog.at_level("WARNING"):
        gateway_client_from_env()

    assert any("明文" in r.message for r in caplog.records)
