"""切单页 / 子 PDF（pypdf，BSD-3-Clause）。引擎按页调用，TextIn 按连续段调用。"""

from __future__ import annotations

import io
from collections.abc import Sequence

from pypdf import PdfReader, PdfWriter


def _reader(pdf_bytes: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        _ = len(reader.pages)
    except Exception as exc:  # pypdf 对坏文件抛多种异常，统一成一句能看懂的话
        raise ValueError(f"不是有效的 PDF：{exc}") from exc
    return reader


def sub_pdf(pdf_bytes: bytes, pages: Sequence[int]) -> bytes:
    reader = _reader(pdf_bytes)
    total = len(reader.pages)
    if not pages:
        raise ValueError("页码列表为空")
    bad = [p for p in pages if not 1 <= p <= total]
    if bad:
        raise ValueError(f"页码越界：{bad}（共 {total} 页）")
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def single_page_pdf(pdf_bytes: bytes, page_no: int) -> bytes:
    return sub_pdf(pdf_bytes, [page_no])
