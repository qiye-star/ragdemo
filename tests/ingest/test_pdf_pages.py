"""本地读 PDF 页数（公告双源接入计划 Task 4）：解析前先知道真实页数，用于费用估算
与 PageBudget 判定，不解析内容、不发网络请求。"""

from __future__ import annotations

from pypdf import PdfWriter

from ragdemo.ingest.pdf_pages import pdf_page_count


def _pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    import io

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_counts_pages_of_a_real_pdf() -> None:
    assert pdf_page_count(_pdf_bytes(3)) == 3


def test_single_page_pdf() -> None:
    assert pdf_page_count(_pdf_bytes(1)) == 1


def test_not_a_pdf_returns_none() -> None:
    assert pdf_page_count(b"this is not a pdf") is None


def test_empty_bytes_returns_none() -> None:
    assert pdf_page_count(b"") is None
