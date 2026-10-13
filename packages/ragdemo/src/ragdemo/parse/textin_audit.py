"""TextIn 缓存 JSON 的逐页数字审计辅助（纯函数；W2，docs/10.12-plan/验收记录/TextIn页数字审计.md）。

只读已缓存的 detail[]，不调任何接口。口径：

- 「入库内容」= blocks_from_detail 实际会入库的条目（content != 1）；表格按 cells 重建，
  合并单元格展开成重复值（与 table_markdown 一致）。跨页表的格子按**格自己的 page_id**
  归页（库里整张表只记首页，逐页审计按格归页更接近数字真实所在页）。
- 页眉页脚（content == 1）单独收集，不进入入库内容。
- 入库数字对文字层的"多余数字"沿用 acceptance 的 P3 口径（多重集合差）。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from ragdemo.parse.local.number_closure import numbers_in

BoxPx = tuple[float, float, float, float]

CAT_MERGE_EXPANSION: Final = "merge_expansion"  # 合并单元格展开成重复值多出来的
CAT_SPACING: Final = "spacing_artifact"  # TextIn 在小数点/千分位逗号两侧插了空白（"9 . 44"）
CAT_ADJACENT_PAGE: Final = "adjacent_page"  # 数值在相邻页的文字层里：条目被归到了错的一页
CAT_LIST_MERGE: Final = "list_number_merge"  # 列表编号 "4." 与下一行年份粘连成 "4.2023"
CAT_TITLE_YEAR: Final = "chart_title_year"  # 图表标题（image_title 条目）里的年份/区间，文字层无
CAT_FORMAT_VARIANT: Final = "format_variant"  # 数值在文字层里有，但符号/括号/百分号/小数尾零不同
CAT_IMAGE_OCR: Final = (
    "image_ocr"  # 数值不在文字层，但 TextIn 的 image 条目（图表/图片 OCR 文字）里有
)
CAT_VALUE_ABSENT: Final = "value_absent"  # 其余：数值在文字层里根本不存在，且不来自 image 条目


@dataclass
class PageStore:
    """一页的 TextIn 产出。"""

    stored: list[str] = field(default_factory=list)  # 入库内容（合并单元格展开）
    unexpanded: list[str] = field(default_factory=list)  # 同上但合并单元格只算一次
    title_text: list[str] = field(default_factory=list)  # sub_type == image_title 的条目文本
    image_text: list[str] = field(default_factory=list)  # type == image 的条目文本（图表/图片 OCR）
    header_footer: list[str] = field(default_factory=list)  # content == 1 的条目文本
    table_boxes: list[BoxPx] = field(default_factory=list)  # 表格在本页的外接框（TextIn 像素）
    item_types: Counter[str] = field(default_factory=Counter)

    @property
    def stored_text(self) -> str:
        return "\n".join(self.stored)

    @property
    def unexpanded_text(self) -> str:
        return "\n".join(self.unexpanded)

    @property
    def image_title_text(self) -> str:
        return "\n".join(self.title_text)

    @property
    def image_ocr_text(self) -> str:
        return "\n".join(self.image_text)

    @property
    def header_footer_text(self) -> str:
        return "\n".join(self.header_footer)


def page_offset(detail: Sequence[Mapping[str, Any]]) -> int:
    """与 textin._page_offset 同口径：page_id 0 基时 +1。"""
    ids = [int(d["page_id"]) for d in detail if d.get("page_id") is not None]
    return 1 if ids and min(ids) == 0 else 0


def _union_px(pos_list: Sequence[Sequence[float]]) -> BoxPx | None:
    pts = [p for p in pos_list if p and len(p) == 8]
    if not pts:
        return None
    xs = [v for p in pts for v in p[0::2]]
    ys = [v for p in pts for v in p[1::2]]
    return (min(xs), min(ys), max(xs), max(ys))


def split_detail_by_page(detail: Sequence[Mapping[str, Any]]) -> tuple[dict[int, PageStore], int]:
    """detail[] → {页码: PageStore}；第二个返回值是没有 page_id 的条目数（归不了页）。"""
    off = page_offset(detail)
    pages: dict[int, PageStore] = {}
    unpaged = 0

    def store(page: int) -> PageStore:
        return pages.setdefault(page, PageStore())

    for item in detail:
        page_raw = item.get("page_id")
        is_hf = int(item.get("content", 0) or 0) == 1
        kind = str(item.get("type", "paragraph"))
        text = str(item.get("text", "")).strip()
        if kind == "table" and not is_hf:
            cells = item.get("cells") or []
            if not str(text) and not cells:
                continue
            by_page: dict[int, list[Mapping[str, Any]]] = {}
            for c in cells:
                cp = c.get("page_id", page_raw)
                if cp is None:
                    unpaged += 1
                    continue
                by_page.setdefault(int(cp) + off, []).append(c)
            for pg, cs in by_page.items():
                st = store(pg)
                st.item_types["table"] += 1
                for c in cs:
                    ct = str(c.get("text", "")).replace("\n", " ").strip()
                    if not ct:
                        continue
                    reps = int(c.get("row_span", 1)) * int(c.get("col_span", 1))
                    st.unexpanded.append(ct)
                    st.stored.extend([ct] * max(reps, 1))
                box = _union_px([c.get("position") or [] for c in cs])
                if box is not None:
                    st.table_boxes.append(box)
            continue
        if page_raw is None:
            unpaged += 1
            continue
        st = store(int(page_raw) + off)
        if is_hf:
            st.item_types["header_footer"] += 1
            if text:
                st.header_footer.append(text)
            continue
        st.item_types[kind] += 1
        if text:
            st.stored.append(text)
            st.unexpanded.append(text)
            if kind == "image":
                st.image_text.append(text)
            if str(item.get("sub_type")) == "image_title":
                st.title_text.append(text)
    return pages, unpaged


def excess(out: Counter[str], src: Counter[str]) -> Counter[str]:
    """out 里多于 src 的部分（多重集合差，只留正数）。"""
    return Counter({n: c - src.get(n, 0) for n, c in out.items() if c > src.get(n, 0)})


def value_key(token: str) -> str:
    """去掉括号/百分号/正负号/千分位/小数尾零后的数值键；解析不了就返回去符号的原串。"""
    t = token.strip("()%").lstrip("-").replace(",", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return t
    return format(d.normalize(), "f")


_SPACED_SEP = re.compile(r"(?<=\d)(?:[ \t]+([.,])[ \t]*|[ \t]*([.,])[ \t]+)(?=\d)")


_LIST_MERGE = re.compile(r"^\d{1,2}\.(?:19|20)\d{2}$")


def heal_spacing(text: str) -> str:
    """TextIn 会把 "9.44" 输出成 "9 . 44"、"5,223,600" 输出成 "5 , 223 , 600"。
    去掉小数点/千分位逗号两侧的空白。"""
    return _SPACED_SEP.sub(lambda m: m.group(1) or m.group(2), text)


def _take(pool: Counter[str], avail: Counter[str]) -> tuple[Counter[str], Counter[str]]:
    """从 pool 里取走 avail 里有的 token（按次数）；返回 (取走的, 剩下的)。"""
    taken: Counter[str] = Counter()
    rest: Counter[str] = Counter()
    for tok, c in pool.items():
        k = min(c, avail.get(tok, 0))
        if k:
            taken[tok] += k
        if c - k:
            rest[tok] += c - k
    return taken, rest


def classify_invented(
    stored_text: str,
    unexpanded_text: str,
    layer_text: str,
    image_text: str = "",
    adjacent_layer_text: str = "",
    title_text: str = "",
) -> dict[str, Counter[str]]:
    """把「入库数字 − 文字层数字」的多余部分分成互斥的几类（见 CAT_*），按此顺序先到先得：

    merge_expansion：展开后多出、不展开就不多的那部分；
    spacing_artifact：把 "9 . 44" / "5 , 223 , 600" 的空白去掉后就消失的那部分；
    chart_title_year：同一 token 在图表标题条目里；
    list_number_merge：形如 "4.2023" 的编号+年份粘连；
    image_ocr：同一 token 在 TextIn 的 image 条目里（图表/图片 OCR）；
    adjacent_page：同一 token 出现在相邻页（±1）的文字层——条目页码归属偏了一页；
    format_variant：数值键在文字层出现过（符号/括号/百分号/尾零不同；最弱的一条，放在精确匹配之后）；
    value_absent：其余，数值哪里都没有。
    """
    src = numbers_in(layer_text)
    stored_inv = excess(numbers_in(stored_text), src)
    raw_base = excess(numbers_in(unexpanded_text), src)
    expansion = stored_inv - raw_base
    healed = excess(numbers_in(heal_spacing(unexpanded_text)), src)
    base = raw_base & healed
    spacing = raw_base - base
    ocr, rest = _take(base, numbers_in(image_text))
    title, rest = _take(rest, numbers_in(title_text))
    adjacent, rest = _take(rest, numbers_in(adjacent_layer_text))
    list_merge = Counter({t: c for t, c in rest.items() if _LIST_MERGE.match(t)})
    rest = rest - list_merge
    layer_keys = {value_key(t) for t in src}
    fmt: Counter[str] = Counter()
    absent: Counter[str] = Counter()
    for tok, c in rest.items():
        (fmt if value_key(tok) in layer_keys else absent)[tok] += c
    return {
        CAT_MERGE_EXPANSION: expansion,
        CAT_SPACING: spacing,
        CAT_FORMAT_VARIANT: fmt,
        CAT_IMAGE_OCR: ocr,
        CAT_ADJACENT_PAGE: adjacent,
        CAT_TITLE_YEAR: title,
        CAT_LIST_MERGE: list_merge,
        CAT_VALUE_ABSENT: absent,
    }


def box_px_to_pt(
    box: BoxPx, page_w_pt: float, page_h_pt: float, img_w: float, img_h: float
) -> BoxPx:
    sx = page_w_pt / img_w if img_w else 1.0
    sy = page_h_pt / img_h if img_h else 1.0
    return (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)


def split_lost(
    layer_text: str,
    out_text: str,
    header_footer_text: str,
    chart_text: str,
    table_word_texts: Sequence[str],
) -> dict[str, int]:
    """文字层里有、入库内容里没有的数字，按去向归类（先到先得）：

    header_footer（TextIn 标为页眉页脚而丢弃，合法）→ chart（图表碎片，本应剥离）→
    table（落在 TextIn 表格框内的文字层词，表格没带上）→ other。
    """
    lost = excess(numbers_in(layer_text), numbers_in(out_text))
    result = {"header_footer": 0, "chart": 0, "table": 0, "other": 0}
    for cat, text in (("header_footer", header_footer_text), ("chart", chart_text)):
        avail = numbers_in(text)
        for tok in list(lost):
            take = min(lost[tok], avail.get(tok, 0))
            if take:
                result[cat] += take
                lost[tok] -= take
                if lost[tok] <= 0:
                    del lost[tok]
    for wt in table_word_texts:
        for tok, c in numbers_in(wt).items():
            take = min(c, lost.get(tok, 0))
            if take:
                result["table"] += take
                lost[tok] -= take
                if lost[tok] <= 0:
                    del lost[tok]
    result["other"] = sum(lost.values())
    return result


def examples(c: Counter[str], limit: int = 5) -> list[str]:
    return sorted(c)[:limit]
