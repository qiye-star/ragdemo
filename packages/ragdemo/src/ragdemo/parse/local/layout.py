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
    rotation: int = 0  # 页面 /Rotate 属性（0 / 90 / 180 / 270）
    rtl_text: bool = (
        False  # 有字符的文本矩阵从右向左走（matrix[0] < 0），此时 pdfplumber 的词序是倒的
    )


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


_RTL_EPSILON = 1e-6  # 浮点噪声（-1e-16）不算从右向左


def unsafe_orientation_reason(layout: PageLayout) -> str | None:
    """页面方向不是正常的横排从左到右时返回原因，否则 None。

    pdfplumber 对旋转 / 倒向文字会把字符序倒过来（"12.5" → "5.21"），而本地链路的自检基准
    （文字层）也是同样倒过来的，数字闭合与字符闭合都发现不了——所以这种页一律退回，不输出。"""
    if layout.rotation % 360 != 0:
        return f"页面带 /Rotate={layout.rotation} 旋转属性，文字层字序不可信"
    if layout.rtl_text:
        return "有文字从右向左排布（文本矩阵翻转），文字层字序不可信"
    if any(not w.upright for w in layout.words):
        return "页面含旋转 / 竖排文字，文字层字序不可信"
    return None


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
            rtl = any(
                float(c["matrix"][0]) < -_RTL_EPSILON
                for c in p.chars
                if c.get("matrix") is not None
            )
            pages.append(
                PageLayout(
                    i,
                    float(p.width),
                    float(p.height),
                    words,
                    p.extract_text() or "",
                    int(p.rotation or 0),
                    rtl,
                )
            )
    return pages
