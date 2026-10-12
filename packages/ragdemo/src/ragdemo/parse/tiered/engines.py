"""页级解析引擎的统一接口（CLAUDE.md §1.5：业务代码不 import 任何引擎 SDK）。

引擎只回答"这一页转写成什么 markdown"；采不采用由 verify.py 的校验决定。"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from ragdemo.parse.local.layout import extract_pages

VISION_ENGINE_NAME: Final = "qwen_vl"

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


class EngineError(RuntimeError):
    """这一页这个引擎给不出可用结果（坏文件、空输出、截断、内容过滤）。路由器据此降级。"""


class EngineUnavailable(EngineError):
    """引擎服务不可达 / 超时 / 5xx。与 EngineError 一样降级，另计入告警。"""


@dataclass(frozen=True)
class EngineOutput:
    markdown: str
    elapsed_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0


@runtime_checkable
class PageEngine(Protocol):
    name: str
    version: str

    def parse_page(self, page_pdf: bytes, *, page_no: int) -> EngineOutput: ...


def strip_images(markdown: str) -> str:
    """引擎输出里的图片引用（文件名常带坐标数字）不是正文；进校验前必须去掉，
    否则会被当成"凭空出现的数字"，造成误拒。"""
    return _HTML_IMG.sub("", _MD_IMAGE.sub("", markdown))


class MockPageEngine:
    """离线测试用：默认回显单页 PDF 的文字层；可按页指定回复、失败或整体不可达。"""

    def __init__(
        self,
        name: str = "mock",
        version: str = "1",
        *,
        replies: Mapping[int, str] | None = None,
        fail_pages: frozenset[int] = frozenset(),
        unavailable: bool = False,
        tokens: tuple[int, int] = (0, 0),
    ) -> None:
        self.name = name
        self.version = version
        self._replies = dict(replies or {})
        self._fail_pages = fail_pages
        self._unavailable = unavailable
        self._tokens = tokens
        self.calls: list[int] = []

    def parse_page(self, page_pdf: bytes, *, page_no: int) -> EngineOutput:
        t0 = time.perf_counter()
        if self._unavailable:
            raise EngineUnavailable(f"{self.name} 不可达（mock）")
        if page_no in self._fail_pages:
            raise EngineError(f"{self.name} 第 {page_no} 页失败（mock）")
        if page_no in self._replies:
            text = self._replies[page_no]
        else:
            try:
                layouts = extract_pages(page_pdf)
            except ValueError as exc:
                raise EngineError(str(exc)) from exc
            if len(layouts) != 1:
                raise EngineError(f"期望单页 PDF，收到 {len(layouts)} 页")
            text = layouts[0].text
        self.calls.append(page_no)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return EngineOutput(strip_images(text), elapsed, self._tokens[0], self._tokens[1])
