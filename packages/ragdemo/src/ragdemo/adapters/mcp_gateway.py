"""MCP 网关客户端（streamable-http）。

网关（windIFinD-mcp）把 Tushare 官方 MCP、万得 Wind、同花顺 iFind、AkShare、
财经新闻五个源聚合成一个端点，是**唯一持有上游凭证**的进程。本模块只做客户端：
按本仓库的既定分工，服务端的部署与加固不在这里。

## 为什么不引 mcp SDK

网关跑的是 `StreamableHTTPSessionManager(stateless=True)`——实测 `initialize`
不返回 `Mcp-Session-Id`，`tools/list` / `tools/call` 不握手也能直接调。没有会话、
没有保活、没有重连语义要维护，协议面就剩「POST 一个 JSON-RPC，解一帧响应」。
为这点东西引一个带 anyio TaskGroup 的 SDK，换来的是绕开本仓库已经加固过的
`HttpClient`——那里有限流、重试、配额、计费，以及把密钥从日志里剥掉的处理。
两相权衡，走 HttpClient。

## 实测出来的三个坑（照协议文档写会全踩）

1. **成功是 SSE 帧，失败可能是裸 JSON。** 成功返回 `event: message` 加一行
   `data: {...}`；而请求被网关自己拒掉时（例如请求体不是合法 UTF-8），回的是
   HTTP 500 加一个**不带帧**的 `{"jsonrpc":"2.0","id":"server-error","error":{...}}`。
   只按 SSE 解析的客户端会在这里抛 StopIteration，把一句可读的错误变成看不懂的崩溃。
2. **HTTP 500 可能是「请求本身不合法」而不是「服务端抖了」。** 实测发一个编码
   损坏的请求体，网关 0.17 秒就回 500——快到根本没走上游，因为它在解析阶段就失败了。
   这类失败重试多少次都一样，而 500 默认就在重试名单里。分类必须看响应体：
   同一个状态码既可能是可重试的抖动，也可能是永远不会变好的请求错误。
3. **`/mcp` 必须带尾斜杠**，否则 307 重定向，POST 体在跳转后可能丢。

## 时点语义

本模块**不产出 `FactRecord`**，只负责把工具调通并留下原始快照。`known_at` 的计算
是每个数据源自己的规则（公告用发布时间、财务用公告日），要接进事实链路时由对应的
`FactAdapter` 实现来定，不能在传输层拍一个 `now()`——那是入库时间不是可获知时间
（CLAUDE.md §1.1）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.errors import AdapterError, UpstreamUnavailable
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket

logger = logging.getLogger(__name__)

PROVIDER = "mcp-gateway"
MCP_ENDPOINT = "/mcp/"  # 尾斜杠不能省，见模块文档坑 3
MCP_PROTOCOL_VERSION = "2025-06-18"

URL_ENV = "RAGDEMO_MCP_GATEWAY_URL"
INSECURE_ENV = "RAGDEMO_MCP_GATEWAY_ALLOW_INSECURE"

# 网关是「本地自建的私有服务」，默认只允许发往私有地址；公网必须上 TLS。
_PRIVATE_HOSTNAME_SUFFIXES = (".local", ".internal", ".cluster.local")
_TRUTHY = frozenset({"1", "true", "TRUE", "yes"})


class McpToolFailed(AdapterError):
    """工具执行了但业务失败（`isError=True`，如积分不足、无权限）。

    与 UpstreamUnavailable 分开是因为重试策略相反：这个重试无益，
    传输失败则可能只是抖动。
    """


class McpProtocolError(AdapterError):
    """响应既不是 SSE 帧也不是 JSON-RPC，解不出来。"""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用的结果。

    `raw` 留着是为了 CLAUDE.md §1.5 的 provider_snapshot：入库时原始响应与
    结构化结果都要存，出了问题才能回溯到底是上游给错了还是我们解错了。
    """

    tool: str
    text: str
    payload: Any
    is_error: bool
    raw: RawResponse


def _decode_frame(body: str) -> dict[str, Any]:
    """把响应体解成 JSON-RPC 对象，同时接受 SSE 帧与裸 JSON。"""
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                frame: dict[str, Any] = json.loads(line[6:])
            except json.JSONDecodeError as exc:
                raise McpProtocolError(f"SSE data 行不是合法 JSON: {line[:120]!r}") from exc
            return frame

    try:
        bare: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise McpProtocolError(f"响应既没有 SSE data 行也不是 JSON: {body[:120]!r}") from exc
    return bare


def _text_of(result: Mapping[str, Any]) -> str:
    """取 result.content 里各 text 块并拼接，非 text 类型跳过。"""
    parts = [
        str(item.get("text", ""))
        for item in result.get("content", [])
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    return "".join(parts)


@runtime_checkable
class GatewayClient(Protocol):
    """`McpGatewayClient` 与 `mock.mcp_gateway.MockMcpGateway` 共同的接口。

    资源注入的调用方（如 `ingest/assets.py::price_normalized`）该按这个
    Protocol 类型标注，不按 `McpGatewayClient` 这个具体类——Mock 不是它的
    子类，只是结构相同，与 `parse/textin.py::DocumentParser` 是同一个模式
    （`TextInParser`/`MockDocumentParser` 也不共享继承关系）。
    """

    provider: str

    def health(self) -> bool: ...
    def list_tools(self) -> list[ToolSpec]: ...
    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolCall: ...


class McpGatewayClient:
    """只做两件事：列工具、调工具。"""

    provider = PROVIDER

    def __init__(self, http: HttpClient, *, endpoint: str = MCP_ENDPOINT) -> None:
        self._http = http
        self._endpoint = endpoint

    def _rpc(self, method: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], RawResponse]:
        raw = self._http.post_text(
            self._endpoint,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params)},
            # 少了 text/event-stream，服务端不会用 SSE 应答。
            headers={"Accept": "application/json, text/event-stream"},
        )
        frame = _decode_frame(str(raw.payload))

        if "error" in frame:
            err = frame["error"] or {}
            raise UpstreamUnavailable(
                f"{PROVIDER} {method} 失败：{err.get('message', '未知错误')}"
                f"（code={err.get('code')}, http={raw.http_status}）"
            )

        result = frame.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(f"{method} 响应缺少 result: {str(raw.payload)[:120]!r}")
        return result, raw

    def health(self) -> bool:
        """能列出工具就算通。/admin/health 不在 MCP 协议里，不依赖它。"""
        try:
            return bool(self.list_tools())
        except AdapterError:
            return False

    def list_tools(self) -> list[ToolSpec]:
        result, _ = self._rpc("tools/list", {})
        return [
            ToolSpec(
                name=str(t["name"]),
                description=str(t.get("description", "")),
                input_schema=dict(t.get("inputSchema", {})),
            )
            for t in result.get("tools", [])
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolCall:
        result, raw = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
        text = _text_of(result)

        if result.get("isError"):
            raise McpToolFailed(f"{PROVIDER} 工具 {name} 业务失败：{text[:300]}")

        try:
            payload: Any = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = None  # 工具返回的不一定是 JSON，保留原文即可

        return ToolCall(tool=name, text=text, payload=payload, is_error=False, raw=raw)


def _is_private_host(host: str) -> bool:
    """不做 DNS 解析：避免联网与 TOCTOU，纯字面判断。

    与 retrieval/vector_index.py 的 Chroma 闸门同一套判据，理由也相同——
    「本地自建」是配置意图，不该靠一次可能变化的解析结果来认定。
    """
    if not host:
        return False
    if host in {"localhost", "::1"}:
        return True
    if "." not in host and ":" not in host:
        return True  # compose 里的裸服务名，如 mcp-gateway
    if host.endswith(_PRIVATE_HOSTNAME_SUFFIXES):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def gateway_client_from_env(*, timeout_s: float = 60.0) -> McpGatewayClient:
    """按环境变量构造客户端。**没有默认地址，也不会去猜。**

    不给默认值是刻意的：把一个明文公网端点写死进仓库，等于让任何 clone 这份代码
    的人默认把查询发到那台机器上。地址属于部署配置，只能来自环境。

    明文 HTTP 发往公网会被拒——查询里带着实体代码与 as_of，响应里是实体财务数据，
    在链路上裸奔违反 CLAUDE.md §0「数据境内存储」的意图。要放行必须显式设
    RAGDEMO_MCP_GATEWAY_ALLOW_INSECURE，且会留下一条 WARNING：静默降级是安全
    事故最常见的开头。

    超时默认 60s 而不是沿用 30s：实测经网关转一次上游要 2.4-3.6s，
    Wind/iFind 的重接口更慢，30s 在批量取数时会把正常请求切断。
    """
    url = os.environ.get(URL_ENV, "").strip()
    if not url:
        raise ValueError(
            f"{URL_ENV} 未设置。MCP 网关地址属于部署配置，仓库里不提供默认值——"
            "写死一个明文公网端点会让任何一份 clone 默认把查询发过去。"
        )

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme == "http" and not _is_private_host(host):
        if os.environ.get(INSECURE_ENV, "").strip() not in _TRUTHY:
            raise ValueError(
                f"{URL_ENV} 指向公网地址 {host!r} 却用明文 HTTP——查询与实体财务数据会在"
                f"链路上裸奔。请给网关加 TLS；确实要放行就显式设 {INSECURE_ENV}=1。"
            )
        logger.warning(
            "MCP 网关走明文 HTTP 发往公网主机 %s，已被 %s 显式放行——"
            "链路上的实体财务数据不受保护，这是临时措施不是配置",
            host,
            INSECURE_ENV,
        )

    http = HttpClient(
        provider=PROVIDER,
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=1.0),
        # 网关自己不限速，但 iFind 有套餐并发硬限（免费 2 / 个人 5 / 企业 10），
        # 超限远端直接拒。保守压住，别把额度浪费在必然被拒的请求上。
        bucket=TokenBucket(rate_per_minute=120),
        timeout_s=timeout_s,
        cost_per_call_cents=Decimal(0),
    )
    return McpGatewayClient(http)


__all__: Sequence[str] = (
    "MCP_ENDPOINT",
    "MCP_PROTOCOL_VERSION",
    "GatewayClient",
    "McpGatewayClient",
    "McpProtocolError",
    "McpToolFailed",
    "ToolCall",
    "ToolSpec",
    "gateway_client_from_env",
)
