"""期别列识别（纯函数，不算任何数值）。

把列表头路径（外层 -> 内层）映射为 `PeriodRef(period, basis)`，供事实匹配使用。
保守原则：任何含糊（多个互相矛盾的期别、变动率列、无法识别的显式日期/区间、
没有年份的季度词）一律返回 None；错误的期别比没有期别更危险。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ragdemo.ingest.stage2_crosscheck import period_of_title

PeriodBasis = Literal["point", "ytd", "single_quarter"]


@dataclass(frozen=True)
class PeriodRef:
    period: str
    basis: PeriodBasis


_PERIOD: Final = re.compile(r"^((?:19|20)[0-9]{2})(Q1|H1|Q3|FY)$")
_RATE_WORDS: Final = ("增减", "变动", "比例", "%", "％", "比上年")
_DASH: Final = "-－—–~～至"
_RANGE: Final = re.compile(rf"((?:19|20)[0-9]{{2}})年(\d{{1,2}})[{_DASH}](\d{{1,2}})月")
_DATE: Final = re.compile(r"((?:19|20)[0-9]{2})年(\d{1,2})月(\d{1,2})日")
_WORD: Final = re.compile(
    r"((?:19|20)[0-9]{2})年?(第一季度|一季度|上半年|半年度|前三季度|(?<!前)第三季度|(?<!前)三季度|度)"
)
_YEAR_ONLY: Final = re.compile(r"((?:19|20)[0-9]{2})年度")
_UNSUPPORTED: Final = re.compile(
    r"(?:19|20)[0-9]{2}年(?:\d{1,2}月|第[一二三四]季度|[一二三四]季度|上半年|下半年|半年度|前三季度)"
    r"|第[一二三四]季度|[一二三四]季度|上半年|下半年|半年度|前三季度"
)
_RANGES: Final[dict[tuple[int, int], tuple[str, PeriodBasis]]] = {
    (1, 3): ("Q1", "ytd"),
    (1, 6): ("H1", "ytd"),
    (1, 9): ("Q3", "ytd"),
    (7, 9): ("Q3", "single_quarter"),
    (1, 12): ("FY", "ytd"),
}
_DATES: Final[dict[tuple[int, int], str]] = {
    (3, 31): "Q1",
    (6, 30): "H1",
    (9, 30): "Q3",
    (12, 31): "FY",
}
_WORDS: Final[dict[str, tuple[str, PeriodBasis]]] = {
    "第一季度": ("Q1", "ytd"),
    "一季度": ("Q1", "ytd"),
    "上半年": ("H1", "ytd"),
    "半年度": ("H1", "ytd"),
    "前三季度": ("Q3", "ytd"),
    "第三季度": ("Q3", "single_quarter"),
    "三季度": ("Q3", "single_quarter"),
}
_REL: Final = re.compile(
    "年初至报告期末|本报告期末|本期末|报告期末|期末余额|上年度末|上年末|期初余额|年初余额"
    "|上年同期(?!末)|上期金额|上期发生额|本报告期|本期金额|本期发生额|本期"
)
_REL_KIND: Final = {
    "年初至报告期末": "ytd",
    "本报告期末": "cur_point",
    "本期末": "cur_point",
    "报告期末": "cur_point",
    "期末余额": "cur_point",
    "上年度末": "prior_point",
    "上年末": "prior_point",
    "期初余额": "prior_point",
    "年初余额": "prior_point",
    "上年同期": "prior",
    "上期金额": "prior",
    "上期发生额": "prior",
    "本报告期": "cur",
    "本期金额": "cur",
    "本期发生额": "cur",
    "本期": "cur",
}


def doc_period_of_title(title: str) -> str | None:
    return period_of_title(title)


def shift_year(period: str, years: int) -> str:
    m = _PERIOD.match(period)
    if m is None:
        raise ValueError(f"not a period: {period!r}")
    return f"{int(m.group(1)) + years}{m.group(2)}"


_AMBIGUOUS: Final = object()


def _explicit(seg: str) -> list[PeriodRef] | object:
    """段内的显式期别；含无法识别的期别文字 -> _AMBIGUOUS。"""
    found: list[PeriodRef] = []
    for m in _RANGE.finditer(seg):
        hit = _RANGES.get((int(m.group(2)), int(m.group(3))))
        if hit is None:
            return _AMBIGUOUS
        found.append(PeriodRef(f"{m.group(1)}{hit[0]}", hit[1]))
    rest = _RANGE.sub(" ", seg)
    for m in _DATE.finditer(rest):
        d = _DATES.get((int(m.group(2)), int(m.group(3))))
        if d is None:
            return _AMBIGUOUS
        found.append(PeriodRef(f"{m.group(1)}{d}", "point"))
    rest = _DATE.sub(" ", rest)
    for m in _WORD.finditer(rest):
        if m.group(2) == "度":  # "2025年度"
            found.append(PeriodRef(f"{m.group(1)}FY", "ytd"))
            continue
        code, basis = _WORDS[m.group(2)]
        found.append(PeriodRef(f"{m.group(1)}{code}", basis))
    rest = _WORD.sub(" ", rest)
    for m in _YEAR_ONLY.finditer(rest):
        found.append(PeriodRef(f"{m.group(1)}FY", "ytd"))
    rest = _YEAR_ONLY.sub(" ", rest)
    if _UNSUPPORTED.search(rest):
        return _AMBIGUOUS
    return found


def _relative(text: str, doc_period: str) -> PeriodRef | None:
    m = _PERIOD.match(doc_period)
    if m is None:
        return None
    kinds = {_REL_KIND[t] for t in _REL.findall(text)}
    is_q3 = m.group(2) == "Q3"
    if "prior" in kinds:
        if kinds - {"prior", "ytd"}:
            return None
        basis: PeriodBasis = "ytd" if "ytd" in kinds or not is_q3 else "single_quarter"
        return PeriodRef(shift_year(doc_period, -1), basis)
    if len(kinds) != 1:
        return None
    kind = next(iter(kinds))
    if kind == "ytd":
        return PeriodRef(doc_period, "ytd")
    if kind == "cur_point":
        return PeriodRef(doc_period, "point")
    if kind == "prior_point":
        return PeriodRef(shift_year(f"{m.group(1)}FY", -1), "point")
    return PeriodRef(doc_period, "single_quarter" if is_q3 else "ytd")


def parse_period_path(path: Sequence[str], *, doc_period: str) -> PeriodRef | None:
    segs = [re.sub(r"\s+", "", s) for s in path]
    if any(w in s for s in segs for w in _RATE_WORDS):
        return None
    explicit: set[PeriodRef] = set()
    for seg in reversed(segs):
        found = _explicit(seg)
        if found is _AMBIGUOUS:
            return None
        assert isinstance(found, list)
        explicit.update(found)
    if explicit:
        joined = "".join(segs)
        if len(explicit) > 1 or any(k in joined for k in ("上年同期", "上期金额", "上期发生额")):
            return None
        return next(iter(explicit))
    return _relative("".join(segs), doc_period)
