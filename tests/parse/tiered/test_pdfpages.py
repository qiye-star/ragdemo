import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from ragdemo.parse.local.layout import extract_pages
from ragdemo.parse.tiered.pdfpages import single_page_pdf, sub_pdf

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")


def _three_pages() -> bytes:
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(FIXTURE.read_bytes())).pages:
        w.add_page(p)
    w.add_blank_page(width=595, height=842)
    w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_sub_pdf_keeps_the_requested_pages_in_order() -> None:
    out = sub_pdf(_three_pages(), [1, 3])
    assert len(PdfReader(io.BytesIO(out)).pages) == 2
    assert "91.2" in extract_pages(out)[0].text


def test_single_page_pdf_has_exactly_one_page() -> None:
    out = single_page_pdf(_three_pages(), 2)
    assert len(PdfReader(io.BytesIO(out)).pages) == 1


def test_out_of_range_pages_and_garbage_are_value_errors() -> None:
    with pytest.raises(ValueError, match="页码越界"):
        sub_pdf(_three_pages(), [4])
    with pytest.raises(ValueError, match="不是有效的 PDF"):
        single_page_pdf(b"not a pdf", 1)
