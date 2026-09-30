"""数字抽取与闭合校验：大模型整理后的文本里，每个数字必须原样出现在 PDF 文字层里。

这是"计算与生成分离"在解析环节的落地：模型只排版，不得产生数值。

括号是 token 的一部分（不转换为负号）；'-' 仅当不紧跟在数字或点之后时才作为符号。
全宽字符和 Unicode 减号通过显式转换表处理（超/下标/圈号不转换，避免误识别）。

注意：字母或 CJK 后的 '-' 被识别为符号（如 EPS-0.5、下降-3.5），可能产生假阳性
（A-1、COVID-19 类），但这仅是虚警，永远不是虚警的反方向。"""

from __future__ import annotations

import re
from collections import Counter

# 全宽字符 + Unicode 减号转换表：仅转换指定的全宽数字、标点和减号变体
# 不转换上标、下标、圈号等其他 Unicode 字符（避免虚警）
_ASCII_FOLD = str.maketrans(
    {
        # 全宽数字 ０-９
        0xFF10: "0",  # ０
        0xFF11: "1",  # １
        0xFF12: "2",  # ２
        0xFF13: "3",  # ３
        0xFF14: "4",  # ４
        0xFF15: "5",  # ５
        0xFF16: "6",  # ６
        0xFF17: "7",  # ７
        0xFF18: "8",  # ８
        0xFF19: "9",  # ９
        # 全宽标点
        0xFF0C: ",",  # ，
        0xFF0E: ".",  # ．
        0xFF05: "%",  # ％
        0xFF08: "(",  # （
        0xFF09: ")",  # ）
        # 减号变体
        0x2212: "-",  # − (Unicode minus)
        0xFF0D: "-",  # － (full-width hyphen-minus)
        0x2010: "-",  # ‐ (hyphen)
        0x2013: "-",  # – (en-dash)
        0x2014: "-",  # — (em-dash)
    }
)

# 括号数字 (3.5%) (1,234) (3.5)% | 负号（仅当不紧跟数字或点） | 其他数字
_NUM = re.compile(
    r"\(\s*-?\d[\d,]*(?:\.\d+)?\s*%?\s*\)%?"  # (3.5%)  (1,234)  (3.5)%
    r"|(?<![0-9.])-?\d[\d,]*(?:\.\d+)?%?"  # -3.5%  91.2  1,234  (includes EPS-0.5)
    r"|\d[\d,]*(?:\.\d+)?%?"  # digits right after a hyphen in ranges/dates
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


def numbers_in(text: str) -> Counter[str]:
    """文本里所有数字的多重集合（归一化后）。年份、页码也算——宁可多校验。

    应用显式字符转换表（全宽字符、Unicode 减号）。
    """
    text = text.translate(_ASCII_FOLD)
    return Counter(normalize_number(m.group()) for m in _NUM.finditer(text))


def closure_violations(structured: str, source: str) -> list[str]:
    """structured 里出现次数多于 source 的数字；空列表表示闭合。按多重集合比较。"""
    src = numbers_in(source)
    out = numbers_in(structured)
    return sorted(n for n, c in out.items() if c > src.get(n, 0))
