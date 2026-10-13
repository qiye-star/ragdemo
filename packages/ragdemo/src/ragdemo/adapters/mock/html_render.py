"""HtmlRenderer 的 Mock：返回 N 页空白 PDF，测试与离线开发用，不依赖任何浏览器。"""

from __future__ import annotations

import io

from pypdf import PdfWriter

from ragdemo.adapters.html_render import RenderedPdf


class MockHtmlRenderer:
    def __init__(self, pages: int = 1) -> None:
        if pages < 1:
            raise ValueError("pages 必须 >= 1")
        self._pages = pages

    def render(self, html: bytes) -> RenderedPdf:
        writer = PdfWriter()
        for _ in range(self._pages):
            writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)
        return RenderedPdf(pdf=buf.getvalue(), renderer="mock-html-renderer:1")
