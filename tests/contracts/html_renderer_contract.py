"""任何 HtmlRenderer 实现（Mock、Edge 无头）都必须通过的契约（EDGAR 8-K 转 PDF 方案 §4.6-1）。"""

from __future__ import annotations

import io

from pypdf import PdfReader

from ragdemo.adapters.html_render import HtmlRenderer

SAMPLE_HTML = (
    b"<html><head><meta charset='utf-8'><title>t</title></head>"
    b"<body><p>Hello 8-K</p><table><tr><td>1,190</td></tr></table></body></html>"
)


class HtmlRendererContract:
    def make_renderer(self) -> HtmlRenderer:
        raise NotImplementedError

    def test_output_is_a_pdf(self) -> None:
        out = self.make_renderer().render(SAMPLE_HTML)
        assert out.pdf.startswith(b"%PDF")

    def test_page_count_is_readable(self) -> None:
        out = self.make_renderer().render(SAMPLE_HTML)
        assert len(PdfReader(io.BytesIO(out.pdf)).pages) >= 1

    def test_renderer_identifier_is_nonempty(self) -> None:
        out = self.make_renderer().render(SAMPLE_HTML)
        assert isinstance(out.renderer, str) and out.renderer
