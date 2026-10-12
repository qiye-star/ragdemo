"""本地版面提取：每页的词坐标与文字层。整个本地解析链路里唯一碰 PDF 库的地方。"""

from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass

import pdfplumber


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    upright: bool = True  # False = 旋转 / 竖排字符（水印、竖排页眉）
    size: float = 0.0  # 字号（pt）：词内字符字号的众数；0 = 未知
    bold: bool = False  # 词内过半字符的字体名含 Bold / Black / Heavy 等

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


_BOLD_MARKERS = ("bold", "black", "heavy", "semibold", "demibold", "-bd", "extrabold")


def _is_bold_font(fontname: str) -> bool:
    name = fontname.lower()
    return any(m in name for m in _BOLD_MARKERS)


def _word_font(chars: list[dict[str, object]]) -> tuple[float, bool]:
    """词内字符字号的众数（0.1pt 精度）与过半字符是否粗体。"""
    sizes = Counter(round(float(str(c.get("size", 0.0))), 1) for c in chars)
    size = sizes.most_common(1)[0][0] if sizes else 0.0
    bold_n = sum(1 for c in chars if _is_bold_font(str(c.get("fontname", ""))))
    return size, bool(chars) and bold_n * 2 > len(chars)


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
                    bool(w.get("upright", True)),
                    *_word_font(w.get("chars", [])),
                )
                for w in p.extract_words(keep_blank_chars=False, return_chars=True)
            ]
            pages.append(
                PageLayout(i, float(p.width), float(p.height), words, p.extract_text() or "")
            )
    return pages
