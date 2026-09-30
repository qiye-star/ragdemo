"""数字抽取与闭合校验：大模型整理后的文本里，每个数字必须原样出现在 PDF 文字层里。

这是"计算与生成分离"在解析环节的落地：模型只排版，不得产生数值。

括号是 token 的一部分（不转换为负号）；'-' 仅当不紧跟在 [0-9A-Za-z.] 后时才作为符号。
所有文本用 NFKC 归一化（处理全宽字符）。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# 括号数字 (3.5%) (1,234) (3.5)% | 负号（仅当不紧跟数字/字母/点） | 其他数字
_NUM = re.compile(
    r"\(\s*-?\d[\d,]*(?:\.\d+)?\s*%?\s*\)%?"  # (3.5%)  (1,234)  (3.5)%
    r"|(?<![0-9A-Za-z.])-?\d[\d,]*(?:\.\d+)?%?"  # -3.5%  91.2  1,234
    r"|\d[\d,]*(?:\.\d+)?%?"  # digits right after a hyphen in ranges/dates
)


def normalize_number(token: str) -> str:
    """去千分位逗号；括号保留为 token 的一部分；保留小数位（不做任何数值四舍五入）。

    NFKC 归一化处理全宽字符。
    """
    t = unicodedata.normalize("NFKC", token)
    t = re.sub(r"[\s,]", "", t)
    if t.startswith("(") and t.endswith(")%"):
        t = t[:-2] + "%)"
    return t


def numbers_in(text: str) -> Counter[str]:
    """文本里所有数字的多重集合（归一化后）。年份、页码也算——宁可多校验。

    NFKC 归一化处理全宽字符。
    """
    text = unicodedata.normalize("NFKC", text)
    return Counter(normalize_number(m.group()) for m in _NUM.finditer(text))


def closure_violations(structured: str, source: str) -> list[str]:
    """structured 里出现次数多于 source 的数字；空列表表示闭合。按多重集合比较。"""
    src = numbers_in(source)
    out = numbers_in(structured)
    return sorted(n for n, c in out.items() if c > src.get(n, 0))
