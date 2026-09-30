"""本地版面提取：每页的词坐标与文字层。整个本地解析链路里唯一碰 PDF 库的地方。"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pdfplumber


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def mid_y(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class PageLayout:
    page: int  # 从 1 起
    width: float
    height: float
    words: list[Word]
    text: str  # 文字层，按阅读顺序


def extract_pages(pdf_bytes: bytes) -> list[PageLayout]:
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception as exc:  # pdfminer 对坏文件抛多种异常，统一成一句能看懂的话
        raise ValueError(f"不是有效的 PDF：{exc}") from exc
    with pdf:
        pages: list[PageLayout] = []
        for i, p in enumerate(pdf.pages, start=1):
            words = [
                Word(
                    str(w["text"]),
                    float(w["x0"]),
                    float(w["x1"]),
                    float(w["top"]),
                    float(w["bottom"]),
                )
                for w in p.extract_words(keep_blank_chars=False)
            ]
            pages.append(
                PageLayout(i, float(p.width), float(p.height), words, p.extract_text() or "")
            )
    return pages
