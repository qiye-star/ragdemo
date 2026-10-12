"""剥离装饰文字（纯函数）：旋转/竖排水印、重复页眉页脚、纯页码行。

目的是在送给大模型之前把这些"装饰字"拿掉：模型常把它们删掉，而字符闭合零容忍，
会因此误判整页失败。被剥离的文字不进入输出，但**完整保存**在 ``StrippedPage.items``
（带原因）与 ``stripped_text`` 里，可审计；剥离是无损拆分（字符不丢不增）。

保守原则（宁可少剥、不可误剥正文）：
- 不足 MIN_DOC_PAGES 页的文档什么都不剥（样本太少，无法判断"重复"）；
- 只看顶 / 底带内的行；正文区的行永远不剥；
- 落在表格区域内的行永远不剥（表头恰在顶带时不能被当页眉）；
- 重复页眉按"行文本"比较，只把页码 token（第 N 页、N/M）归一化；
  其他数字不同的行（如每页变化的价格行）视为数据，不剥。"""

from __future__ import annotations

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

ROTATED_MAX_SHARE = (
    0.5  # 旋转字符占页面字符超过这个比例 → 整页是横排扫描 / 横向版面，不是水印，不剥
)
REASON_ROTATED = "rotated"
REASON_HEADER = "header"
REASON_FOOTER = "footer"
REASON_PAGE_NUMBER = "page_number"

_PAGE_NUMBER_ROW = re.compile(
    r"^(?:第\s*\d+\s*页(?:\s*[,，]?\s*共\s*\d+\s*页)?"  # 第 3 页 / 第 3 页 共 12 页
    r"|\d+"  # 3
    r"|\d+\s*/\s*\d+"  # 3 / 12
    r"|-\s*\d+\s*-"  # - 3 -
    r"|[—–]\s*\d+\s*[—–])$"  # — 3 —
)
_INLINE_PAGE_TOKEN = re.compile(r"第\s*\d+\s*页(?:\s*[,，]?\s*共\s*\d+\s*页)?|\b\d+\s*/\s*\d+\b")
_PAGE_KEY = "<PN>"


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


def _row_key(text: str) -> str:
    """页内行的比较键：空白折叠；整行是页码则归一成 <PN>，行内的页码 token 也归一。"""
    collapsed = " ".join(text.split())
    if _PAGE_NUMBER_ROW.match(collapsed):
        return _PAGE_KEY
    return " ".join(_INLINE_PAGE_TOKEN.sub(_PAGE_KEY, collapsed).split())


def _body_text(words: Sequence[Word]) -> str:
    return "\n".join(_row_text(r) for r in cluster_rows(words))


def strip_decor(pages: Sequence[PageLayout]) -> list[StrippedPage]:
    """对整份文档剥离装饰文字，返回与输入等长的列表。"""
    if len(pages) < MIN_DOC_PAGES:
        return [StrippedPage(p) for p in pages]

    # 第一遍：每页找出候选带内行（排除旋转字与表格区域内的行）
    per_page: list[tuple[list[Word], list[Word], list[tuple[str, list[Word]]]]] = []
    seen: dict[tuple[str, str], set[int]] = {}
    for p in pages:
        rotated = [w for w in p.words if not w.upright]
        upright = [w for w in p.words if w.upright]
        if sum(len(w.text) for w in rotated) > ROTATED_MAX_SHARE * sum(
            len(w.text) for w in p.words
        ):
            rotated, upright = [], list(p.words)  # 整页旋转：当作正文
        rows = cluster_rows(upright)
        table_ids = {id(w) for region in find_table_regions(rows) for r in region for w in r}
        bands: list[tuple[str, list[Word]]] = []
        for row in rows:
            if any(id(w) in table_ids for w in row):
                continue
            mid = sum(w.mid_y for w in row) / len(row)
            if mid <= TOP_BAND * p.height:
                side = "top"
            elif mid >= (1 - BOTTOM_BAND) * p.height:
                side = "bottom"
            else:
                continue
            bands.append((side, row))
            seen.setdefault((side, _row_key(_row_text(row))), set()).add(p.page)
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
            key = _row_key(text)
            reason: str | None = None
            if side == "bottom" and _PAGE_NUMBER_ROW.match(" ".join(text.split())):
                reason = REASON_PAGE_NUMBER  # 纯页码行：不要求重复
            elif len(seen[(side, key)]) >= threshold:
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
                PageLayout(p.page, p.width, p.height, body, _body_text(body)),
                items,
            )
        )
    return out
