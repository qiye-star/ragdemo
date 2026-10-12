"""数字抽取与闭合校验：大模型整理后的文本里，每个数字必须原样出现在 PDF 文字层里。

这是"计算与生成分离"在解析环节的落地：模型只排版，不得产生数值。

括号是 token 的一部分（不转换为负号）；'-' 仅当不紧跟在数字或点之后时才作为符号。
全宽字符和 Unicode 减号通过显式转换表处理（超/下标/圈号不转换，避免误识别）。

注意：字母或 CJK 后的 '-' 被识别为符号（如 EPS-0.5、下降-3.5），可能产生假阳性
（A-1、COVID-19 类），但这仅是虚警，永远不是虚警的反方向。"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter

# 全宽字符 + Unicode 减号转换表：仅转换指定的全宽数字、标点和减号变体
# 不转换上标、下标、圈号等其他 Unicode 字符（避免虚警）
_FOLD_MAP: dict[int, str] = {
    # 全宽数字 ０-９
    **{0xFF10 + i: str(i) for i in range(10)},
    # 全宽标点
    0xFF0C: ",",  # ，
    0xFF0E: ".",  # ．
    0xFF05: "%",  # ％
    0xFF08: "(",  # （
    0xFF09: ")",  # ）
    0x2212: "-",  # − (Unicode minus)
    0x02D7: "-",  # ˗ (modifier letter minus sign)
    0x2796: "-",  # ➖ (heavy minus sign)
    0x2043: "-",  # ⁃ (hyphen bullet，类别 Po，不在 Pd 内)
}
# 所有 Unicode 破折号标点（类别 Pd：连字符、en/em dash、figure dash、small/full-width 变体……）
# 一律折叠为减号：任何一种都可能是 PDF 文字层里的负号，丢掉符号就是改了数值。
# 由 unicodedata 计算（随 Python 的 Unicode 版本更新），不用手写清单。
_FOLD_MAP.update(
    {c: "-" for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Pd"}
)
# 不转换上标、下标、圈号等其他 Unicode 字符（避免虚警）
_ASCII_FOLD = str.maketrans(_FOLD_MAP)

# 整数部分：逗号只有在后面恰好跟 3 位数字时才是千分位（"3,5" 是两个数，不是 35）
_INT = r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)"

# 括号数字 (3.5%) (1,234) (3.5)% | 负号（仅当不紧跟数字或点） | 其他数字
_BODY = rf"(?:{_INT}(?:\.\d+)?|\.\d+)"  # 91.2  1,234  .5（前导小数点属于数字本身）
_NUM = re.compile(
    rf"\(\s*-?{_BODY}\s*%?\s*\)%?"  # (3.5%)  (1,234)  (.5)%
    rf"|(?<![0-9.])-?{_BODY}%?"  # -3.5%  91.2  1,234  -.5  (includes EPS-0.5)
    rf"|{_BODY}%?"  # digits right after a hyphen in ranges/dates
)


def normalize_number(token: str) -> str:
    """去千分位逗号；括号保留为 token 的一部分；保留小数位（不做任何数值四舍五入）。

    应用显式字符转换表（全宽字符、Unicode 减号）。
    """
    t = token.translate(_ASCII_FOLD)
    t = re.sub(r"[\s,]", "", t)
    if t.startswith("(") and t.endswith(")%"):
        t = t[:-2] + "%)"
    return t


def fold_text(text: str) -> str:
    """全宽/减号变体折叠（单字符到单字符，长度不变，位置可对应）。"""
    return text.translate(_ASCII_FOLD)


def number_spans(text: str) -> list[tuple[int, int, str]]:
    """折叠后文本里每个数字的 (起, 止, 归一化 token)，按阅读顺序。位置对应 fold_text(text)。"""
    return [
        (m.start(), m.end(), normalize_number(m.group())) for m in _NUM.finditer(fold_text(text))
    ]


def ordered_numbers(text: str) -> list[str]:
    """与 numbers_in 同一套分词与归一化，但保留阅读顺序与重复。"""
    return [tok for _, _, tok in number_spans(text)]


def numbers_in(text: str) -> Counter[str]:
    """文本里所有数字的多重集合（归一化后）。年份、页码也算——宁可多校验。

    应用显式字符转换表（全宽字符、Unicode 减号）。
    """
    return Counter(ordered_numbers(text))


def closure_violations(structured: str, source: str) -> list[str]:
    """structured 里出现次数多于 source 的数字；空列表表示闭合。按多重集合比较。"""
    src = numbers_in(source)
    out = numbers_in(structured)
    return sorted(n for n, c in out.items() if c > src.get(n, 0))
