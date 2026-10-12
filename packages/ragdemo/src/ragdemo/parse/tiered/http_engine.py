"""自托管解析引擎（PaddleOCR / MinerU / Docling）的 HTTP 适配器。

引擎本体跑在本地 docker（infra/parse-engines/<name>），容器里只有我们自己的薄壳暴露统一 JSON
协议；业务代码不 import 任何引擎 SDK（CLAUDE.md §1.5）。协议：
  GET  /v1/health      -> {"engine", "engine_version", "device"}
  POST /v1/parse-page  {"pdf_base64", "page_no"}
                       -> {"ok": true, "markdown", "elapsed_ms"} | {"ok": false, "error"}
内容层失败返回 200 + ok=false，不与传输层故障混在同一套状态码分类里。

EngineError 只在引擎自报失败（ok=false）或响应缺 markdown 字段时抛出。引擎成功返回空 markdown
是合法响应：本适配器照常返回，由下游 verify 因文字层数字缺失而拒绝，不在这里判空。
输出统一经 normalize_markup 去掉引擎的包装标签与展示性属性（不改任何文字内容）。"""

from __future__ import annotations

import base64
import time
from typing import Any, Literal

import httpx

from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.http import HttpClient, ProviderConfig, RetryPolicy, TokenBucket
from ragdemo.parse.tiered.engines import EngineError, EngineOutput, EngineUnavailable, strip_images
from ragdemo.parse.tiered.markup_normalize import normalize_markup

EngineName = Literal["paddle", "mineru", "docling"]
HEALTH = "/v1/health"
PARSE = "/v1/parse-page"


class HttpPageEngine:
    def __init__(self, http: HttpClient, *, name: str, version: str) -> None:
        self._http = http
        self.name = name
        self.version = version

    def parse_page(self, page_pdf: bytes, *, page_no: int) -> EngineOutput:
        t0 = time.perf_counter()
        body = {"pdf_base64": base64.b64encode(page_pdf).decode("ascii"), "page_no": page_no}
        try:
            raw = self._http.post_json(PARSE, body)
        except AdapterError as exc:
            raise EngineUnavailable(f"{self.name} 不可达：{exc}") from exc
        payload: Any = raw.payload
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            err = payload.get("error") if isinstance(payload, dict) else "响应不是 JSON 对象"
            raise EngineError(f"{self.name} 第 {page_no} 页失败：{err}")
        markdown = payload.get("markdown")
        if not isinstance(markdown, str):
            raise EngineError(f"{self.name} 第 {page_no} 页响应缺少 markdown")
        elapsed = payload.get("elapsed_ms")
        if isinstance(elapsed, int) and elapsed >= 0:
            ms = elapsed
        else:
            ms = int((time.perf_counter() - t0) * 1000)
        return EngineOutput(normalize_markup(strip_images(markdown)), ms)


def engine_from_url(
    name: EngineName,
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    retry: RetryPolicy | None = None,
) -> HttpPageEngine:
    cfg = ProviderConfig.load(f"parse_{name}")
    http = HttpClient(
        provider=f"parse_{name}",
        base_url=base_url,
        policy=retry or cfg.retry,
        bucket=TokenBucket(rate_per_minute=cfg.rate_limit_qpm),
        timeout_s=cfg.timeout_s,
        daily_quota=cfg.daily_quota,
        transport=transport,
    )
    try:
        health: Any = http.get_json(HEALTH, {}).payload
    except AdapterError as exc:
        raise EngineUnavailable(f"{name} 健康检查失败：{exc}") from exc
    if (
        not isinstance(health, dict)
        or health.get("engine") != name
        or not health.get("engine_version")
    ):
        raise EngineUnavailable(f"{name} 健康检查返回异常：{health!r}")
    return HttpPageEngine(http, name=name, version=str(health["engine_version"]))
