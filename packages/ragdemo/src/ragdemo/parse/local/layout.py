"""本地版面提取：每页的词坐标与文字层。整个本地解析链路里唯一碰 PDF 库的地方。"""

from __future__ import annotations

import io
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pdfplumber


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    upright: bool = (
        True  # False = 不水平：旋转 / 竖排 / 倾斜 / 倒向（由字符矩阵判定，见 char_is_horizontal）
    )
    size: float = 0.0  # 字号（pt）：词内字符字号的众数；0 = 未知
    bold: bool = False  # 词内过半字符的字体名含 Bold / Black / Heavy 等

    @property
    def mid_y(self) -> float:
        return (self.top + self.bottom) / 2


GraphicKind = Literal["image", "rect", "line", "curve"]


@dataclass(frozen=True)
class Graphic:
    """页面上的一个非文字对象的外接框（pdfplumber 坐标：原点页面左上角，y 向下，单位 pt）。"""

    kind: GraphicKind
    x0: float
    x1: float
    top: float
    bottom: float
    filled: bool = False  # rect / curve 是否填充（pdfplumber 对象的 fill）
    points: int = 0  # curve 的点数（pdfplumber 的 pts）；其他为 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top


def _graphic(kind: GraphicKind, o: Mapping[str, Any]) -> Graphic:
    return Graphic(
        kind,
        float(o["x0"]),
        float(o["x1"]),
        float(o["top"]),
        float(o["bottom"]),
        bool(o.get("fill", False)),
        len(o.get("pts") or []) if kind == "curve" else 0,
    )


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
    graphics: tuple[Graphic, ...] = ()  # 图片 / 矩形 / 线 / 曲线的外接框（图表检测用）


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
MIN_HORIZONTAL_SCALE = 1e-6  # 文本矩阵 a 必须大于它：a 在 0 附近（含 ±4.4e-08 噪声）说明文字是竖的
MAX_TILT_DEGREES = 2.0  # 允许的最大倾斜角；超过算"不水平"（pdfminer 的 upright 把 ±90° 内都算直立）
_MAX_TILT_TAN = math.tan(math.radians(MAX_TILT_DEGREES))


def char_is_horizontal(matrix: Sequence[float]) -> bool:
    """字符文本矩阵 (a, b, c, d, e, f) 是否是安全的"从左到右的水平文字"。

    只有 a > MIN_HORIZONTAL_SCALE、|b| <= tan(2°)·a 且 d > 0 才安全；剪切 c != 0（合成斜体）没问题。
    不能信 pdfminer 的 upright 标志：它大致是 a·d > 0 且 b·c <= 0，于是 -90°~90° 之间任意倾斜
    （60°、89°）与带浮点噪声的 90° 矩阵都算直立；而 pdfplumber 对这类文字的字序是乱的 / 倒的，
    自检基准（文字层）同样如此，闭合校验看不出来。"""
    a, b, _c, d = (float(x) for x in matrix[:4])
    return a > MIN_HORIZONTAL_SCALE and abs(b) <= _MAX_TILT_TAN * a and d > 0


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


def _word_is_horizontal(w: dict[str, Any]) -> bool:
    """词的 upright：pdfplumber 的标志与每个字符的矩阵判定都通过才算（字段名沿用 upright）。"""
    if not bool(w.get("upright", True)):
        return False
    return all(
        char_is_horizontal(c["matrix"]) for c in w.get("chars", []) if c.get("matrix") is not None
    )


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
                    _word_is_horizontal(w),
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
                    tuple(
                        [_graphic("image", o) for o in p.images]
                        + [_graphic("rect", o) for o in p.rects]
                        + [_graphic("line", o) for o in p.lines]
                        + [_graphic("curve", o) for o in p.curves]
                    ),
                )
            )
    return pages
