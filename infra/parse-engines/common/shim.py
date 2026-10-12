"""解析引擎薄壳：stdlib HTTP 服务，单线程串行（引擎实例不保证线程安全）。

只在容器内网监听；不记录请求体（文档内容）。引擎 SDK 只在各引擎的 server.py 里 import。"""

from __future__ import annotations

import base64
import json
import tempfile
import time
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ParseFn = Callable[[Path], str]  # 单页 PDF 路径 -> markdown
MAX_BODY = 32 * 1024 * 1024  # 请求体上限；单页 PDF 远小于此
REQUEST_TIMEOUT_S = 120  # 慢速/挂起的连接最多占住单线程服务这么久


class RequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_length(header: str | None) -> int:
    """Content-Length 必须存在、是正整数且不超过上限；在读取请求体之前校验。"""
    try:
        length = int(header or "")
    except ValueError:
        raise RequestError(400, "缺少或非法的 Content-Length") from None
    if length <= 0:
        raise RequestError(400, "Content-Length 必须为正")
    if length > MAX_BODY:
        raise RequestError(413, "请求体过大")
    return length


def decode_pdf(body: bytes) -> bytes:
    try:
        raw = base64.b64decode(json.loads(body)["pdf_base64"], validate=True)
    except Exception as exc:  # noqa: BLE001
        raise RequestError(400, f"请求格式错误：{type(exc).__name__}") from None
    if not raw.startswith(b"%PDF"):
        raise RequestError(400, "不是有效的 PDF")
    return raw


def count_pages(raw: bytes) -> int:
    import pypdfium2  # 随 paddleocr[doc-parser] 安装；只在容器内使用

    return len(pypdfium2.PdfDocument(raw))


def require_single_page(raw: bytes, counter: Callable[[bytes], int] = count_pages) -> None:
    try:
        pages = counter(raw)
    except Exception:  # noqa: BLE001
        raise RequestError(400, "PDF 无法读取") from None
    if pages != 1:
        raise RequestError(400, f"每次请求必须恰好一页，收到 {pages} 页")


def serve(engine: str, version: str, device: str, parse_fn: ParseFn, port: int = 8000) -> None:
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def _send(self, status: int, body: dict[str, object]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 约定的方法名
            if self.path != "/v1/health":
                self._send(404, {"error": "not found"})
                return
            self._send(200, {"engine": engine, "engine_version": version, "device": device})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/parse-page":
                self._send(404, {"error": "not found"})
                return
            try:
                length = parse_length(self.headers.get("Content-Length"))
                raw = decode_pdf(self.rfile.read(length))
                require_single_page(raw)
            except RequestError as exc:
                self._send(exc.status, {"ok": False, "error": str(exc)})
                return
            t0 = time.perf_counter()
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "page.pdf"
                path.write_bytes(raw)
                try:
                    markdown = parse_fn(path)
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    self._send(200, {"ok": False, "error": f"引擎异常：{type(exc).__name__}"})
                    return
            self._send(200, {"ok": True, "markdown": markdown, "elapsed_ms": int((time.perf_counter() - t0) * 1000)})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # 不写访问日志：路径固定，正文不得落日志

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 — 只在 internal 网络内可达
