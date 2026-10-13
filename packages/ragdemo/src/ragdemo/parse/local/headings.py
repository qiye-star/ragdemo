"""编号标题规则（纯函数）：识别中文公告 / 研报的章节编号，
校验同一文档内编号的连续性，按编号体系定级。

只判断"哪一行是标题、第几级"；加 `#` 由 typeset 完成，任何字符都不增删。
连续性校验是精确率的主要保障："3.5亿元……"这类以数字开头的正文行即使长得像 `1.1`，
也会因为前面没有 `3.4`（或不是该层的第 1 个）而被剔除。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

NumberingSystem = Literal[
    "chapter",
    "section",
    "cn",
    "cn_paren",
    "arabic",
    "decimal2",
    "decimal3",
    "arabic_paren",
    "keyword",
]
# 层级先后（GB/T 9704 惯例：第X章 > 第X节 > 一、 > （一） > 1. > 1.1 > 1.1.1 > （1））。
SYSTEM_ORDER: Final[tuple[NumberingSystem, ...]] = (
    "chapter",
    "section",
    "cn",
    "cn_paren",
    "arabic",
    "decimal2",
    "decimal3",
    "arabic_paren",
)
MIN_SYSTEM_HEADINGS = 2  # 一个编号体系在全文至少保留这么多个，才算"成体系"
MAX_LEVEL = 6
UNIT_AFTER_NUMBER: Final = "亿万千百元%％倍个家人台年月日"

_CN = "一二三四五六七八九十零〇"
_PATTERNS: Final[tuple[tuple[NumberingSystem, re.Pattern[str]], ...]] = (
    ("chapter", re.compile(rf"^第([{_CN}]+)章")),
    ("section", re.compile(rf"^第([{_CN}]+)节")),
    ("cn", re.compile(rf"^([{_CN}]{{1,3}})[、．.](?=\S)")),
    ("cn_paren", re.compile(rf"^[（(]([{_CN}]{{1,3}})[）)](?=\S)")),
    ("decimal3", re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})(?![\d.])\s*(?=\S)")),
    ("decimal2", re.compile(r"^(\d{1,2})\.(\d{1,2})(?![\d.])\s*(?=\S)")),
    ("arabic", re.compile(r"^(\d{1,2})[、．.](?!\d)\s*(?=\S)")),
    ("arabic_paren", re.compile(r"^[（(](\d{1,2})[）)]\s*(?=\S)")),
)
_KEYWORD = re.compile(
    r"^(?:(?:特别提示|重要提示|重要内容提示|释义|目录|备查文件目录|备查文件|风险提示)[：:]?"
    r"|附件[一二三四五六七八九十\d]{0,3}(?:[：:].{0,30})?)$"
)
_CN_DIGITS: Final = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass(frozen=True)
class Numbering:
    system: NumberingSystem
    ordinal: int  # keyword 为 0
    scope: tuple[int, ...] = ()  # 小数编号的前缀（1.2.3 → (1, 2)），其余为空


def cn_numeral(s: str) -> int | None:
    """一 … 九十九。不支持"百"（章节号不会这么大）。"""
    if not s:
        return None
    if "十" in s:
        tens_s, _, ones_s = s.partition("十")
        tens = _CN_DIGITS.get(tens_s) if tens_s else 1
        ones = _CN_DIGITS.get(ones_s) if ones_s else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return _CN_DIGITS.get(s) if len(s) == 1 else None


def numbering_of(text: str) -> Numbering | None:
    s = text.strip()
    if _KEYWORD.match(s):
        return Numbering("keyword", 0)
    for system, pat in _PATTERNS:
        m = pat.match(s)
        if m is None:
            continue
        rest = s[m.end() :].lstrip()
        if (
            system in ("decimal2", "decimal3", "arabic")
            and rest[:1]
            and rest[0] in UNIT_AFTER_NUMBER
        ):
            return None  # "3.5亿元""1.5倍"：带单位的数值，不是编号
        groups = m.groups()
        if system in ("chapter", "section", "cn", "cn_paren"):
            n = cn_numeral(groups[0])
            return None if n is None else Numbering(system, n)
        nums = tuple(int(g) for g in groups)
        return Numbering(system, nums[-1], nums[:-1])
    return None


@dataclass(frozen=True)
class Candidate:
    index: int  # 调用方的行序号（文档顺序）
    numbering: Numbering


def keep_consistent(cands: Sequence[Candidate]) -> frozenset[int]:
    """按文档顺序保留编号连续的候选：同一体系同一前缀内，第一个必须是 1，之后必须 +1；
    出现更高一级的标题时，所有更低级体系的计数清零。最后剔除不足 MIN_SYSTEM_HEADINGS 个的体系。"""
    kept: list[Candidate] = []
    last: dict[tuple[NumberingSystem, tuple[int, ...]], int] = {}
    for c in cands:
        n = c.numbering
        if n.system == "keyword":
            kept.append(c)
            continue
        key = (n.system, n.scope)
        prev = last.get(key)
        if not ((prev is None and n.ordinal == 1) or (prev is not None and n.ordinal == prev + 1)):
            continue
        rank = SYSTEM_ORDER.index(n.system)
        for k in [k for k in last if SYSTEM_ORDER.index(k[0]) > rank]:
            del last[k]
        last[key] = n.ordinal
        kept.append(c)
    per_system = Counter(c.numbering.system for c in kept)
    return frozenset(
        c.index
        for c in kept
        if c.numbering.system == "keyword" or per_system[c.numbering.system] >= MIN_SYSTEM_HEADINGS
    )


def assign_levels(systems: Sequence[NumberingSystem], *, base: int) -> dict[NumberingSystem, int]:
    """base = 字号标题已用掉的级数。文档里出现的编号体系按 SYSTEM_ORDER 依次占 base+1、base+2……；
    关键词标题（释义、重要内容提示……）与最高一级编号同级。"""
    present = sorted({s for s in systems if s != "keyword"}, key=SYSTEM_ORDER.index)
    levels: dict[NumberingSystem, int] = {
        s: min(base + 1 + i, MAX_LEVEL) for i, s in enumerate(present)
    }
    levels["keyword"] = min(base + 1, MAX_LEVEL)
    return levels
