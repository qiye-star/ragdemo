"""任何 PageEngine 实现（Mock、自托管引擎 HTTP 适配器、视觉接口适配器）都必须通过的契约。

用法：子类化 PageEngineContract，实现 make_engine()。真实适配器用录制回放的
httpx.MockTransport 跑同一套契约；连真实服务的版本标 @pytest.mark.live_engine。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragdemo.parse.local.compare import plain_text_from_markdown
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.tiered.engines import EngineError, PageEngine
from ragdemo.parse.tiered.pdfpages import single_page_pdf

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")


class PageEngineContract:
    def make_engine(self) -> PageEngine:
        raise NotImplementedError

    def sample_page(self) -> bytes:
        return single_page_pdf(FIXTURE.read_bytes(), 1)

    def test_name_and_version_are_nonempty_strings(self) -> None:
        e = self.make_engine()
        assert isinstance(e.name, str) and e.name
        assert isinstance(e.version, str) and e.version

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(self.make_engine(), PageEngine)

    def test_a_valid_page_yields_markdown_with_its_numbers(self) -> None:
        out = self.make_engine().parse_page(self.sample_page(), page_no=1)
        nums = numbers_in(plain_text_from_markdown(out.markdown))
        assert {"91.2", "60.1", "19.3", "12.7"} <= set(nums)
        assert out.elapsed_ms >= 0 and out.prompt_tokens >= 0 and out.completion_tokens >= 0

    def test_output_carries_no_image_references(self) -> None:
        md = self.make_engine().parse_page(self.sample_page(), page_no=1).markdown
        assert "![" not in md and "<img" not in md.lower()

    def test_garbage_bytes_raise_engine_error(self) -> None:
        with pytest.raises(EngineError):
            self.make_engine().parse_page(b"not a pdf", page_no=1)
