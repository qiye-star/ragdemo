"""本地解析 vs TextIn 的对比指标（纯函数）。注意：TextIn 的结果不是绝对标准答案，
指标只说明"两路是否一致"；不一致处由人工抽查裁定（实验报告里记录）。"""

from __future__ import annotations

import html
import itertools
import re
from collections import Counter
from dataclasses import dataclass

from ragdemo.parse.local.number_closure import numbers_in

_WS = re.compile(r"\s+")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_TABLE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
_TR = re.compile(r"<tr\b.*?</tr>", re.DOTALL | re.IGNORECASE)


def plain_text_from_markdown(md: str) -> str:
    """TextIn 缓存的 markdown 里表格是 HTML：去掉注释与所有标签（属性数字不得进入文本）。"""
    text = _COMMENT.sub("", md)
    text = _BR.sub(" ", text)
    text = _TAG.sub(" ", text)
    return html.unescape(text)


def text_coverage(reference: str, candidate: str) -> float:
    ref = Counter(_WS.sub("", reference))
    if not ref:
        return 1.0
    cand = Counter(_WS.sub("", candidate))
    hit = sum(min(c, cand.get(ch, 0)) for ch, c in ref.items())
    return hit / sum(ref.values())


def number_recall_precision(reference: str, candidate: str) -> tuple[float, float]:
    ref, cand = numbers_in(reference), numbers_in(candidate)
    hit = sum(min(c, cand.get(n, 0)) for n, c in ref.items())
    recall = hit / sum(ref.values()) if ref else 1.0
    precision = hit / sum(cand.values()) if cand else 1.0
    return recall, precision


def _pipe_rows(md: str) -> list[Counter[str]]:
    """每张表（连续的 `|` 行）跳过第一行（表头），其余行含 >= 2 个数字才算数据行。

    表头里的年份（2024 / 2023）是数字，但不是数据；若把它算进去，"数据行全部串行"
    的表仍会因为表头一致而得到非零召回，指标就骗人了。"""
    rows: list[Counter[str]] = []
    header_pending = True
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            header_pending = True  # 遇到非表格行：下一张表从头开始
            continue
        if set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue  # 分隔行 |---|---|
        if header_pending:
            header_pending = False
            continue
        nums = numbers_in(line)
        if sum(nums.values()) >= 2:
            rows.append(nums)
    return rows


def _html_rows(md: str) -> list[Counter[str]]:
    """HTML 表格：每张表第一个 <tr> 是表头（跳过），其余行含 >= 2 个数字才算数据行。"""
    rows: list[Counter[str]] = []
    for table in _TABLE.findall(_COMMENT.sub("", md)):
        for i, tr in enumerate(_TR.findall(table)):
            if i == 0:
                continue
            nums = numbers_in(plain_text_from_markdown(tr))
            if sum(nums.values()) >= 2:
                rows.append(nums)
    return rows


def _table_rows(md: str) -> list[Counter[str]]:
    return _pipe_rows(_TABLE.sub("\n", md)) + _html_rows(md)


def table_row_recall(reference_md: str, candidate_md: str) -> float:
    ref_rows = _table_rows(reference_md)
    if not ref_rows:
        return 1.0
    cand_rows = _table_rows(candidate_md)
    hit = sum(
        1 for r in ref_rows if any(all(c.get(n, 0) >= k for n, k in r.items()) for c in cand_rows)
    )
    return hit / len(ref_rows)


_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")


@dataclass(frozen=True)
class HeadingMatch:
    """标题按"规范化文本"（只留字母数字，去空白与标点）的多重集合匹配；层级另算。"""

    matched: int
    cand_total: int
    ref_total: int
    level_agree: int  # 匹配到的标题里层级也相同的个数

    @property
    def recall(self) -> float:
        return self.matched / self.ref_total if self.ref_total else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.cand_total if self.cand_total else 1.0

    @property
    def level_agreement(self) -> float:
        return self.level_agree / self.matched if self.matched else 1.0


def _headings(md: str) -> list[tuple[str, int]]:
    """markdown 里的 `#` 标题行 → (规范化文本, 层级)。HTML 表格先整体去掉；标签去掉。"""
    out: list[tuple[str, int]] = []
    for line in _TABLE.sub("\n", _COMMENT.sub("", md)).splitlines():
        m = _HEADING_LINE.match(line.strip())
        if not m:
            continue
        key = "".join(ch for ch in plain_text_from_markdown(m.group(2)) if ch.isalnum())
        if key:
            out.append((key, len(m.group(1))))
    return out


def heading_matches(candidate_md: str, reference_md: str) -> HeadingMatch:
    cand, ref = _headings(candidate_md), _headings(reference_md)
    cand_text, ref_text = Counter(k for k, _ in cand), Counter(k for k, _ in ref)
    cand_pair, ref_pair = Counter(cand), Counter(ref)
    return HeadingMatch(
        matched=sum(min(c, ref_text.get(k, 0)) for k, c in cand_text.items()),
        cand_total=len(cand),
        ref_total=len(ref),
        level_agree=sum(min(c, ref_pair.get(kl, 0)) for kl, c in cand_pair.items()),
    )


def heading_recall_precision(candidate_md: str, reference_md: str) -> tuple[float, float]:
    m = heading_matches(candidate_md, reference_md)
    return m.recall, m.precision


def heading_level_agreement(candidate_md: str, reference_md: str) -> float:
    return heading_matches(candidate_md, reference_md).level_agreement


def table_data_rows(md: str) -> list[Counter[str]]:
    """表格数据行（每表跳过首行、行内 >= 2 个数字）；pipe 与 HTML 表格都算。"""
    return _table_rows(md)


@dataclass(frozen=True)
class HeadingMatchV2:
    """v2：两行拆开的标题与参考里合成一行的标题算匹配；层级按相对层级（相邻匹配对的级差符号）比较。"""

    matched: int
    cand_total: int
    ref_total: int
    rel_agree: int
    rel_total: int

    @property
    def recall(self) -> float:
        return self.matched / self.ref_total if self.ref_total else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.cand_total if self.cand_total else 1.0

    @property
    def relative_level_agreement(self) -> float:
        return self.rel_agree / self.rel_total if self.rel_total else 1.0


def _merge_split_titles(
    cand: list[tuple[str, int]], ref_keys: Counter[str]
) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    i = 0
    while i < len(cand):
        if i + 1 < len(cand):
            (a, la), (b, lb) = cand[i], cand[i + 1]
            if la == lb and a not in ref_keys and b not in ref_keys and (a + b) in ref_keys:
                out.append((a + b, la))
                i += 2
                continue
        out.append(cand[i])
        i += 1
    return out


def _aligned_pairs(
    cand: list[tuple[str, int]], ref: list[tuple[str, int]]
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    j = 0
    for i, (key, _) in enumerate(cand):
        for jj in range(j, len(ref)):
            if ref[jj][0] == key:
                pairs.append((i, jj))
                j = jj + 1
                break
    return pairs


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


def heading_matches_v2(candidate_md: str, reference_md: str) -> HeadingMatchV2:
    ref = _headings(reference_md)
    ref_keys = Counter(k for k, _ in ref)
    cand = _merge_split_titles(_headings(candidate_md), ref_keys)
    cand_keys = Counter(k for k, _ in cand)
    matched = sum(min(c, ref_keys.get(k, 0)) for k, c in cand_keys.items())
    agree = total = 0
    for (i1, j1), (i2, j2) in itertools.pairwise(_aligned_pairs(cand, ref)):
        total += 1
        agree += int(_sign(cand[i2][1] - cand[i1][1]) == _sign(ref[j2][1] - ref[j1][1]))
    return HeadingMatchV2(matched, len(cand), len(ref), agree, total)
