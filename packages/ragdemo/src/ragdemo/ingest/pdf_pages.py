"""本地读 PDF 页数（公告双源接入计划 Task 4）。

只读页数，不解析内容、不发网络请求——`dry_run=True` 时用它给出**真实**页数与费用
估算（区别于按标题档位猜的经验值），比"季报大约 12 页"这类估算更可信，因为 PDF
已经下载在手上，读页数几乎零成本。

读不出（不是合法 PDF、加密、损坏）返回 `None`，调用方退回经验估算，不让一份坏文件
挡住整批 dry run。
"""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError


def pdf_page_count(body: bytes) -> int | None:
    if not body:
        return None
    try:
        return len(PdfReader(io.BytesIO(body)).pages)
    except (PdfReadError, ValueError, KeyError):
        return None
