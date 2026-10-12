"""剥离装饰文字（纯函数）：旋转/竖排水印、重复页眉页脚、页码行。

目的是在送给大模型之前把这些"装饰字"拿掉：模型常把它们删掉，而字符闭合零容忍，
会因此误判整页失败。被剥离的文字不进入输出，但**完整保存**在 ``StrippedPage.items``
（带原因）与 ``stripped_text`` 里，可审计；剥离是无损拆分（字符不丢不增）。

保守原则（宁可少剥、不可误剥正文；拿不准就保留）：
- 不足 MIN_DOC_PAGES 页的文档什么都不剥（样本太少，无法判断"重复"）；
- 只看顶 / 底带内的行；正文区的行永远不剥；
- 落在表格区域内的行永远不剥（表头恰在顶带时不能被当页眉）；
- 与相邻行在 x 上对齐的多段行（疑似 2 列表的表头，``min_cols=3`` 的表格检测抓不到）永远不剥；
- 重复页眉按"行文本"比较，只把"N 等于本页页码"的页码 token（第 N 页、N/M）归一化；
  其他数字不同的行（如每页变化的价格行、审议比例 3/4、7/9……）视为数据，不剥；
- 整行是孤立整数（3、- 3 -）只有在"数值 - 页序号"于 >= 重复阈值的页数上恒定时才算页码
  （页脚里单独成行的 4810、4817…是数据，不是页码）；整行 `第 N 页` / `N/M` 要求 N 等于本页页码；
- 旋转文字只是被拆出来；页面是否安全由下游的方向检查（layout.unsafe_orientation_reason）判定。"""

from __future__ import annotations

import dataclasses
import itertools
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.tables import cluster_rows, find_table_regions

MIN_DOC_PAGES = 4  # 少于这个页数不剥
TOP_BAND = 0.08  # 页高的顶部这一比例视为页眉带
BOTTOM_BAND = 0.08  # 页高的底部这一比例视为页脚带
MIN_REPEAT_PAGES = 3  # 重复行至少出现的页数
REPEAT_FRACTION = 0.5  # ……且至少占总页数的这一比例
SEGMENT_GAP = 8.0  # 与 tables.find_table_regions 同一口径：词间空隙 >= 此值算分段
ALIGN_TOL = 3.0  # 两行的分段起点相差不超过这个距离算 x 对齐
MIN_ALIGNED_SEGMENTS = 2  # 至少这么多个分段起点对齐才算疑似表头

ROTATED_MAX_SHARE = (
    0.5  # 旋转字符占页面字符超过这个比例 → 整页是横排扫描 / 横向版面，不是水印，不剥
)
REASON_ROTATED = "rotated"
REASON_HEADER = "header"
REASON_FOOTER = "footer"
REASON_PAGE_NUMBER = "page_number"

# 整行页码：A 类自带页码（第 N 页 / N/M），B 类是孤立整数（N / - N - / — N —）
_FORM_A_ROW = re.compile(r"^(?:第\s*(\d+)\s*页(?:\s*[,，]?\s*共\s*\d+\s*页)?|(\d+)\s*/\s*\d+)$")
_FORM_B_ROW = re.compile(r"^(?:(\d+)|-\s*(\d+)\s*-|[—–]\s*(\d+)\s*[—–])$")
_INLINE_PAGE_TOKEN = re.compile(
    r"第\s*(\d+)\s*页(?:\s*[,，]?\s*共\s*\d+\s*页)?|\b(\d+)\s*/\s*\d+\b"
)
_PAGE_KEY = "<PN>"

_Band = tuple[str, list[Word]]


@dataclass(frozen=True)
class StrippedPage:
    layout: PageLayout  # 剥离后的正文版面（未剥时就是原对象）
    items: list[tuple[str, str]] = field(default_factory=list)  # (原因, 文字)

    @property
    def stripped_text(self) -> str:
        return "\n".join(text for _, text in self.items)

    @property
    def stripped_chars(self) -> int:
        return sum(len(t.replace(" ", "")) for _, t in self.items)


def _row_text(row: Sequence[Word]) -> str:
    return " ".join(w.text for w in row)


def _first_int(m: re.Match[str]) -> int:
    return int(next(g for g in m.groups() if g is not None))


def _own_page_number(text: str) -> int | None:
    """A 类整行页码（第 N 页 / N/M）里的 N；不是 A 类返回 None。"""
    m = _FORM_A_ROW.match(" ".join(text.split()))
    return _first_int(m) if m else None


def _lone_integer(text: str) -> int | None:
    """B 类整行（孤立整数）的数值；不是返回 None。"""
    m = _FORM_B_ROW.match(" ".join(text.split()))
    return _first_int(m) if m else None


def _row_key(text: str, page: int) -> str:
    """页内行的比较键：空白折叠；行内页码 token 只有在 N 等于本页页码时才归一成 <PN>。"""
    collapsed = " ".join(text.split())
    own = _own_page_number(collapsed)
    if own is not None and own == page:
        return _PAGE_KEY

    def sub(m: re.Match[str]) -> str:
        return _PAGE_KEY if _first_int(m) == page else m.group(0)

    return " ".join(_INLINE_PAGE_TOKEN.sub(sub, collapsed).split())


def _segment_starts(row: Sequence[Word]) -> list[float]:
    starts = [row[0].x0]
    for a, b in itertools.pairwise(row):
        if b.x0 - a.x1 >= SEGMENT_GAP:
            starts.append(b.x0)
    return starts


def _looks_like_table_header(rows: Sequence[Sequence[Word]], i: int) -> bool:
    """多段行，且相邻行（上或下）有 >= MIN_ALIGNED_SEGMENTS 个分段起点与它对齐。"""
    starts = _segment_starts(rows[i])
    if len(starts) < MIN_ALIGNED_SEGMENTS:
        return False
    for j in (i - 1, i + 1):
        if 0 <= j < len(rows):
            other = _segment_starts(rows[j])
            aligned = sum(1 for s in starts if any(abs(s - o) <= ALIGN_TOL for o in other))
            if aligned >= MIN_ALIGNED_SEGMENTS and len(other) >= MIN_ALIGNED_SEGMENTS:
                return True
    return False


def _body_text(words: Sequence[Word]) -> str:
    return "\n".join(_row_text(r) for r in cluster_rows(words))


def strip_decor(pages: Sequence[PageLayout]) -> list[StrippedPage]:
    """对整份文档剥离装饰文字，返回与输入等长的列表。"""
    if len(pages) < MIN_DOC_PAGES:
        return [StrippedPage(p) for p in pages]

    # 第一遍：每页找出候选带内行（排除旋转字、表格区域内的行与疑似表头行）
    per_page: list[tuple[list[Word], list[Word], list[_Band]]] = []
    seen: dict[tuple[str, str], set[int]] = {}
    offsets: dict[tuple[str, int], set[int]] = {}  # (侧, 数值 - 页序号) -> 出现的页
    for p in pages:
        rotated = [w for w in p.words if not w.upright]
        upright = [w for w in p.words if w.upright]
        if sum(len(w.text) for w in rotated) > ROTATED_MAX_SHARE * sum(
            len(w.text) for w in p.words
        ):
            rotated, upright = [], list(p.words)  # 整页旋转：当作正文（下游方向检查会退回该页）
        rows = cluster_rows(upright)
        table_ids = {id(w) for region in find_table_regions(rows) for r in region for w in r}
        bands: list[_Band] = []
        for i, row in enumerate(rows):
            if any(id(w) in table_ids for w in row):
                continue
            mid = sum(w.mid_y for w in row) / len(row)
            if mid <= TOP_BAND * p.height:
                side = "top"
            elif mid >= (1 - BOTTOM_BAND) * p.height:
                side = "bottom"
            else:
                continue
            if _looks_like_table_header(rows, i):
                continue  # 拿不准就保留
            bands.append((side, row))
            text = _row_text(row)
            value = _lone_integer(text)
            if value is not None:
                offsets.setdefault((side, value - p.page), set()).add(p.page)
            else:
                seen.setdefault((side, _row_key(text, p.page)), set()).add(p.page)
        per_page.append((rotated, upright, bands))

    threshold = max(MIN_REPEAT_PAGES, math.ceil(REPEAT_FRACTION * len(pages)))
    out: list[StrippedPage] = []
    for p, (rotated, upright, bands) in zip(pages, per_page, strict=True):
        items: list[tuple[str, str]] = []
        dropped: set[int] = set()
        if rotated:
            items.append((REASON_ROTATED, _row_text(rotated)))
        for side, row in bands:
            text = _row_text(row)
            reason: str | None = None
            own = _own_page_number(text)
            value = _lone_integer(text)
            if own is not None and own == p.page:
                reason = REASON_PAGE_NUMBER  # 第 N 页 / N/M，且 N 就是本页页码：无需重复
            elif value is not None:
                if len(offsets[(side, value - p.page)]) >= threshold:
                    reason = REASON_PAGE_NUMBER  # 数值随页序号同步递增：是页码
            else:
                key = _row_key(text, p.page)
                if len(seen[(side, key)]) >= threshold:
                    if key == _PAGE_KEY:
                        reason = REASON_PAGE_NUMBER
                    else:
                        reason = REASON_HEADER if side == "top" else REASON_FOOTER
            if reason is not None:
                items.append((reason, text))
                dropped.update(id(w) for w in row)
        if not items:
            out.append(StrippedPage(p))
            continue
        body = [w for w in upright if id(w) not in dropped]
        out.append(
            StrippedPage(
                dataclasses.replace(p, words=body, text=_body_text(body)),
                items,
            )
        )
    return out
