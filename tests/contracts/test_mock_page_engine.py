from __future__ import annotations

import pytest

from ragdemo.parse.tiered.engines import EngineError, EngineUnavailable, MockPageEngine
from tests.contracts.page_engine_contract import PageEngineContract


class TestMockPageEngine(PageEngineContract):
    def make_engine(self) -> MockPageEngine:
        return MockPageEngine()


def test_replies_bypass_pdf_parsing_and_fail_pages_raise() -> None:
    e = MockPageEngine(
        replies={2: "第二页 ![](imgs/img_12_34.jpg) 91.2"},
        fail_pages=frozenset({3}),
        tokens=(7, 3),
    )
    out = e.parse_page(b"anything", page_no=2)
    assert out.markdown.strip() == "第二页  91.2"
    assert (out.prompt_tokens, out.completion_tokens) == (7, 3)
    with pytest.raises(EngineError):
        e.parse_page(b"anything", page_no=3)
    assert e.calls == [2]


def test_unavailable_is_a_subclass_of_engine_error() -> None:
    with pytest.raises(EngineUnavailable):
        MockPageEngine(unavailable=True).parse_page(b"x", page_no=1)
    assert issubclass(EngineUnavailable, EngineError)
