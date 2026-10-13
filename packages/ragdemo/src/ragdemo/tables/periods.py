"""期别列识别（纯函数，不算任何数值）。

把列表头路径（外层 -> 内层）映射为 `PeriodRef(period, basis)`，供事实匹配使用。
保守原则：任何含糊（多个互相矛盾的期别、变动率列、无法识别的显式日期/区间、
没有年份的季度词）一律返回 None；错误的期别比没有期别更危险。
"""

from __future__ import annotations

import re
import unicodedata
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
_RATE_WORDS: Final = (
    "增减", "变动", "比例", "%", "比上年", "较上年", "较上期", "增长", "占比", "比重",
    "毛利率", "差额", "差异", "同比", "环比",
)  # fmt: skip
_DASH: Final = "-－—–~～至"
_RANGE: Final = re.compile(rf"((?:19|20)[0-9]{{2}})年(\d{{1,2}})[{_DASH}](\d{{1,2}})月")
_BARE_RANGE: Final = re.compile(rf"[(（]?(\d{{1,2}})月?[{_DASH}](\d{{1,2}})月份?[)）]?")
_DATE: Final = re.compile(r"((?:19|20)[0-9]{2})年(\d{1,2})月(\d{1,2})日")
_WORD: Final = re.compile(
    r"((?:19|20)[0-9]{2})年?(第一季度|一季度|上半年|半年度|前三季度|(?<!前)第三季度|(?<!前)三季度|度)"
)
_YEAR_ONLY: Final = re.compile(r"((?:19|20)[0-9]{2})年度")
_ANY_YEAR: Final = re.compile(r"(?:19|20)[0-9]{2}")
_UNSUPPORTED: Final = re.compile(
    r"(?:19|20)[0-9]{2}年(?:\d{1,2}月|第[一二三四]季度|[一二三四]季度|上半年|下半年|半年度|前三季度)"
    r"|第[一二三四]季度|[一二三四]季度|上半年|下半年|半年度|前三季度"
)
_BAD_SUFFIX: Final = ("累计", "末", "止", "期末")
_RELATIVE_HINT: Final = re.compile("本报告期|本期|上年|上期|年初|报告期|期末|期初|同期")
_MOVEMENT: Final = re.compile(
    "增加|减少|计提|转回|转销|处置|购买|确认|收回|支付|摊销|其他|汇兑|累计|截至"
)
_UNIT_BRACKET: Final = re.compile(r"[(（]\s*(?:人民币)?(?:万|百万|亿)?元\s*[)）]")
_OK_RESIDUE: Final = frozenset({"", "金额", "数", "发生额", "余额"})
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
    "年初至报告期末|本报告期末|本期末|报告期末|(?<![上年])期末余额|上年度末|上年末"
    "|(?<![上年])期初余额|年初余额|上年同期(?!末)|上期金额|上期发生额|本报告期"
    "|本期金额|本期发生额|本期数|本期"
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
    "本期数": "cur",
    "本期": "cur",
}
_EXPECT_RANGE: Final = {"Q1": (1, 3), "H1": (1, 6), "Q3": (1, 9), "FY": (1, 12)}


def doc_period_of_title(title: str) -> str | None:
    return period_of_title(title)


def shift_year(period: str, years: int) -> str:
    m = _PERIOD.match(period)
    if m is None:
        raise ValueError(f"not a period: {period!r}")
    return f"{int(m.group(1)) + years}{m.group(2)}"


_AMBIGUOUS: Final = object()


def _bad_suffix(seg: str, end: int) -> bool:
    return seg[end:].startswith(_BAD_SUFFIX)


def _explicit(seg: str) -> list[PeriodRef] | object:
    """段内的显式期别；含无法识别/带累计、期末、止后缀的期别文字 -> _AMBIGUOUS。"""
    found: list[PeriodRef] = []
    for m in _RANGE.finditer(seg):
        hit = _RANGES.get((int(m.group(2)), int(m.group(3))))
        if hit is None or _bad_suffix(seg, m.end()):
            return _AMBIGUOUS
        found.append(PeriodRef(f"{m.group(1)}{hit[0]}", hit[1]))
    rest = _RANGE.sub(" ", seg)
    for m in _DATE.finditer(rest):
        d = _DATES.get((int(m.group(2)), int(m.group(3))))
        if d is None or _bad_suffix(rest, m.end()):
            return _AMBIGUOUS
        found.append(PeriodRef(f"{m.group(1)}{d}", "point"))
    rest = _DATE.sub(" ", rest)
    for m in _WORD.finditer(rest):
        if _bad_suffix(rest, m.end()):
            return _AMBIGUOUS
        if m.group(2) == "度":  # "2025年度"
            found.append(PeriodRef(f"{m.group(1)}FY", "ytd"))
            continue
        code, basis = _WORDS[m.group(2)]
        found.append(PeriodRef(f"{m.group(1)}{code}", basis))
    rest = _WORD.sub(" ", rest)
    for m in _YEAR_ONLY.finditer(rest):
        if _bad_suffix(rest, m.end()):
            return _AMBIGUOUS
        found.append(PeriodRef(f"{m.group(1)}FY", "ytd"))
    rest = _YEAR_ONLY.sub(" ", rest)
    if _UNSUPPORTED.search(rest):
        return _AMBIGUOUS
    if found:  # 句子/带别的日期的文字不是列表头
        residue = re.sub(r"[(（][^()（）0-9]*[)）]", "", rest)
        residue = re.sub(r"[()（）\s、,，/:：]", "", residue)
        if residue not in _OK_RESIDUE:
            return _AMBIGUOUS
    return found


def _relative(segs: list[str], doc_period: str | None) -> PeriodRef | None:
    m = _PERIOD.match(doc_period or "")
    if m is None or doc_period is None:
        return None
    if any(_ANY_YEAR.search(s) for s in segs):
        return None  # 带年份却没解析出显式期别：含糊
    kinds: set[str] = set()
    ranges: list[tuple[int, int]] = []
    others: list[str] = []
    for raw in segs:
        seg = _UNIT_BRACKET.sub("", raw)
        for r in _BARE_RANGE.finditer(seg):
            ranges.append((int(r.group(1)), int(r.group(2))))
        seg = _BARE_RANGE.sub("", seg)
        tokens = _REL.findall(seg)
        if not tokens:
            others.append(seg)
            continue
        residue = re.sub(r"[()（）\s、,，/:：]", "", _REL.sub("", seg))
        if residue not in _OK_RESIDUE:
            return None
        kinds.update(_REL_KIND[t] for t in tokens)
    if not kinds or any(_MOVEMENT.search(o) for o in others):
        return None
    is_q3 = m.group(2) == "Q3"
    if "prior" in kinds:
        if kinds - {"prior", "ytd"}:
            return None
        basis: PeriodBasis = "ytd" if "ytd" in kinds or not is_q3 else "single_quarter"
        ref = PeriodRef(shift_year(doc_period, -1), basis)
    elif len(kinds) != 1:
        return None
    else:
        kind = next(iter(kinds))
        if kind == "ytd":
            ref = PeriodRef(doc_period, "ytd")
        elif kind == "cur_point":
            ref = PeriodRef(doc_period, "point")
        elif kind == "prior_point":
            ref = PeriodRef(shift_year(f"{m.group(1)}FY", -1), "point")
        else:
            ref = PeriodRef(doc_period, "single_quarter" if is_q3 else "ytd")
    if ranges:
        expect = (7, 9) if ref.basis == "single_quarter" else _EXPECT_RANGE.get(ref.period[4:])
        if ref.basis == "point" or any(r != expect for r in ranges):
            return None
    return ref


def parse_period_path(path: Sequence[str], *, doc_period: str | None) -> PeriodRef | None:
    segs = [
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", re.sub(r"<br\s*/?>", "", s, flags=re.I)))
        for s in path
    ]
    joined = "".join(segs)
    if any(w in joined for w in _RATE_WORDS) or any(s.endswith("率") for s in segs):
        return None
    explicit: set[PeriodRef] = set()
    for seg in segs:
        found = _explicit(seg)
        if found is _AMBIGUOUS:
            return None
        assert isinstance(found, list)
        explicit.update(found)
    if explicit:
        if len(explicit) > 1 or _RELATIVE_HINT.search(joined) or _MOVEMENT.search(joined):
            return None
        return next(iter(explicit))
    return _relative(segs, doc_period)
