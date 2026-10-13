from __future__ import annotations

import io

from pypdf import PdfReader

from ragdemo.adapters.mock.html_render import MockHtmlRenderer
from tests.contracts.html_renderer_contract import SAMPLE_HTML, HtmlRendererContract


class TestMockHtmlRenderer(HtmlRendererContract):
    def make_renderer(self) -> MockHtmlRenderer:
        return MockHtmlRenderer()

    def test_page_count_is_configurable(self) -> None:
        out = MockHtmlRenderer(pages=6).render(SAMPLE_HTML)
        assert len(PdfReader(io.BytesIO(out.pdf)).pages) == 6
