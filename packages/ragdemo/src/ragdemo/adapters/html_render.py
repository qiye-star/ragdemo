"""HTML -> PDF 离线渲染适配器（EDGAR 8-K 转 PDF 方案 §4.5）。

EDGAR 主文档是 htm，没有"页"的概念；TextIn 只吃 PDF，页码引用也要对着 PDF。这里把渲染
抽象成 `HtmlRenderer` 协议（CLAUDE.md 1.5：业务代码不绑定具体渲染器）。

渲染完全离线、免费：不发任何网络请求，也不调用 TextIn。渲染结果**不可逐字节复现**
（PDF 里带创建时间戳），所以调用方必须"渲染一次、存起来、以存下来的为准"。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pypdf import PdfReader


class HtmlRenderError(RuntimeError):
    """渲染失败（找不到渲染器、渲染器超时/退出码非零、没产出 PDF）。"""


@dataclass(frozen=True)
class RenderedPdf:
    pdf: bytes
    # 渲染器标识（含版本），写进来源快照；换机器/升级后分页可能不同，靠它事后追溯。
    renderer: str


@runtime_checkable
class HtmlRenderer(Protocol):
    def render(self, html: bytes) -> RenderedPdf: ...


EDGE_PATH_ENV = "RAGDEMO_EDGE_PATH"
_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_EDG_VERSION = re.compile(r"Edg/([0-9][0-9.]*)")


def renderer_label_from_pdf_creator(creator: str | None) -> str:
    """Edge 打印出的 PDF 的 /Creator 里带 `Edg/<版本>`，用它做渲染器版本标识。"""
    if creator:
        m = _EDG_VERSION.search(creator)
        if m:
            return f"msedge-headless:{m.group(1)}"
    return "msedge-headless:unknown"


def _wait_for_pdf(path: Path, deadline: float) -> bytes | None:
    """等 PDF 落盘完整：文件存在、大小连续两次一致且以 %%EOF 结尾。超时返回 None。"""
    last_size = -1
    while time.monotonic() < deadline:
        if path.is_file():
            size = path.stat().st_size
            if size > 0 and size == last_size:
                data = path.read_bytes()
                if data.rstrip().endswith(b"%%EOF"):
                    return data
            last_size = size
        time.sleep(0.3)
    return None


class EdgeHeadlessRenderer:
    """本机 Edge 无头 `--print-to-pdf`。路径取自环境变量 `RAGDEMO_EDGE_PATH`，
    没设就按常见安装位置与 PATH 查找。"""

    def __init__(self, edge_path: str | None = None, *, timeout_s: float = 120.0) -> None:
        self._edge_path = edge_path
        self._timeout_s = timeout_s

    def _locate(self) -> str:
        explicit = self._edge_path or os.environ.get(EDGE_PATH_ENV)
        if explicit:
            if not Path(explicit).is_file():
                raise HtmlRenderError(f"找不到 Edge 可执行文件：{explicit}")
            return explicit
        for cand in _EDGE_CANDIDATES:
            if Path(cand).is_file():
                return cand
        found = shutil.which("msedge")
        if found:
            return found
        raise HtmlRenderError(f"找不到 Edge：请设置环境变量 {EDGE_PATH_ENV} 指向 msedge.exe")

    def render(self, html: bytes) -> RenderedPdf:
        edge = self._locate()
        # Windows 上 Edge 的启动器进程会先于 PDF 落盘退出，浏览器子进程可能还占着
        # profile 目录，所以清理失败要忽略（只是临时目录）。
        with tempfile.TemporaryDirectory(
            prefix="ragdemo-render-", ignore_cleanup_errors=True
        ) as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "in.htm"
            out = tmp_dir / "out.pdf"
            src.write_bytes(html)
            cmd = [
                edge,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                "--disable-extensions",
                "--no-first-run",
                # 强制离线：除 file:// 之外任何主机名都解析失败，渲染期间不可能外联。
                "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--user-data-dir={tmp_dir / 'profile'}",
                f"--print-to-pdf={out}",
                src.as_uri(),
            ]
            deadline = time.monotonic() + self._timeout_s
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=self._timeout_s, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise HtmlRenderError(f"Edge 渲染超时（{self._timeout_s}s）") from exc
            pdf = _wait_for_pdf(out, deadline)
            if pdf is None:
                raise HtmlRenderError(
                    f"Edge 没有产出 PDF（退出码 {proc.returncode}，等待 {self._timeout_s}s）："
                    f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
                )
        if not pdf.startswith(b"%PDF"):
            raise HtmlRenderError("Edge 产出的文件不是 PDF")
        meta = PdfReader(io.BytesIO(pdf)).metadata
        creator = str(meta.creator) if meta is not None and meta.creator else None
        return RenderedPdf(pdf=pdf, renderer=renderer_label_from_pdf_creator(creator))
