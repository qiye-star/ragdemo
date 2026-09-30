"""数字抽取与闭合校验：大模型整理后的文本里，每个数字必须原样出现在 PDF 文字层里。

这是"计算与生成分离"在解析环节的落地：模型只排版，不得产生数值。"""

from __future__ import annotations

import re
from collections import Counter

# 带千分位 / 小数 / 百分号 / 括号负数；不含中文数字。
_NUM = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?%?")


def normalize_number(token: str) -> str:
    """去千分位逗号；括号负数统一成 -x；保留小数位（不做任何数值四舍五入）。"""
    t = token.strip()
    negative = (t.startswith("(") and t.endswith(")")) or t.startswith("(-")
    t = t.strip("()").replace(",", "")
    if t.startswith("-"):
        negative, t = True, t[1:]
    return f"-{t}" if negative else t


def numbers_in(text: str) -> Counter[str]:
    """文本里所有数字的多重集合（归一化后）。年份、页码也算——宁可多校验。"""
    return Counter(normalize_number(m.group()) for m in _NUM.finditer(text))


def closure_violations(structured: str, source: str) -> list[str]:
    """structured 里出现次数多于 source 的数字；空列表表示闭合。按多重集合比较。"""
    src = numbers_in(source)
    out = numbers_in(structured)
    return sorted(n for n, c in out.items() if c > src.get(n, 0))
