"""图表区域识别与图表文字剥离（纯函数，只看几何与文字层）。

图表里的数据不提取（用户 2026-10-13 裁决）：识别区域 → 区域内文字碎片从正文流无损剥离（记入
stripped_text，与页眉页脚同一机制）→ 区域交给 parse/figures.py 裁图入库。表格优先：落在表格网格或
坐标表格区域里的词永不剥离，绘图框里只要有一个表格词，整块区域放弃。闭合校验不做任何豁免。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from ragdemo.parse.figures import BBox, Detection, FigureRegion
from ragdemo.parse.local.decor import BOTTOM_BAND, TOP_BAND, body_text
from ragdemo.parse.local.layout import Graphic, PageLayout, Word
from ragdemo.parse.local.tables import cluster_rows, find_table_regions, split_segments

CHART_DETECTOR_VERSION: Final = "chart-v1"
# 嵌入图片
IMG_MIN_AREA_SHARE = 0.01
IMG_MIN_SIDE_PT = 36.0
IMG_MAX_AREA_SHARE = 0.60
BAND_MAX_AREA_SHARE = 0.05
SQUARE_MAX_SIDE_PT = 100.0
SQUARE_ASPECT: Final = (0.8, 1.25)
# 矢量图
VECTOR_JOIN_PT = 6.0
VECTOR_MIN_ELEMENTS = 4
VECTOR_MIN_AREA_SHARE = 0.01
RULE_MAX_THICKNESS_PT = 1.5
MIN_BARS = 3
CURVE_MIN_POINTS = 3
MIN_DIAGONALS = 3
BACKGROUND_AREA_SHARE = 0.25
# 表格网格
GRID_SPAN_SHARE = 0.8
GRID_MIN_H_RULES = 3
GRID_MIN_V_RULES = 2
# 标签与图注
LABEL_MARGIN_EM = 3.0
LABEL_MAX_CHARS = 12
FRAGMENT_MAX_CHARS = 6
FRAGMENT_NUMERIC_SHARE = 0.5
FRAGMENT_SEGMENT_GAP = 8.0
CAPTION_MAX_GAP_EM = 4.0
DEDUP_OVERLAP = 0.8
RESIDUAL_MIN_FRAGMENT_ROWS = 3
# 绘图框内部 >= 这么多行是多段（对齐的文字列）→ 是表格不是图表（逻辑规则，见调参记录 §2）
TABLE_LIKE_MIN_ROWS = 3
# 框内 >= 这么多行是"文字标签 + 数字"（营业收入 91.2 60.1）→ 也是表格（T4 评审 C1，规则修复）
LABEL_NUMBER_MIN_ROWS = 2
# 标签 / 框内文字低于正文字号这个比例才可能是图表刻度；与正文同字号的短行是正文（T4 评审 C3）
BODY_SIZE_SHARE = 0.95
REASON_FIGURE: Final = "figure"
REASON_FIGURE_ROTATED: Final = "图表区域内的非水平文字含数字"
REASON_CHART_UNRESOLVED: Final = "图表碎片残留（未检出图表区域）"
_CAPTION = re.compile(r"^(?:图表?|Figure|Fig\.|Chart)\s*[0-9０-９一二三四五六七八九十]+")
_SOURCE_NOTE = re.compile(r"^(?:资料来源|数据来源|来源)\s*[:：]")
_NUMERICISH = frozenset("0123456789.%-+()（）,，％−")
# 句子 / 列表项 / 脚注的形状：编号、项目符号、"注1："开头，或以句末标点结尾（T4 评审 C3）
_LIST_OR_NOTE = re.compile(
    r"^\s*(?:\d{1,2}[、.．)）](?!\d)|[①-⑩]|[(（]\d{1,2}[)）]|[•·▪※]|注\d+|注\s*[:：]|Note)"
)
_SENTENCE_END = re.compile(r"(?:[。；！？;!?]|[A-Za-z一-鿿]\.)$")
_CJK_COMMA = re.compile(r"[一-鿿]，[一-鿿]")


@dataclass(frozen=True)
class ChartStrip:
    layout: PageLayout  # 剥离图表文字后的正文版面
    figures: tuple[FigureRegion, ...] = ()
    items: tuple[tuple[str, str], ...] = ()  # (REASON_FIGURE, 行文字)，与 StrippedPage.items 同形
    discarded: tuple[str, ...] = ()  # 放弃的候选区域与原因（与表格冲突、区域内有正文行）
    unsafe_reason: str | None = None

    @property
    def stripped_text(self) -> str:
        return "\n".join(t for _, t in self.items)

    @property
    def stripped_words(self) -> int:
        return sum(f.stripped_words for f in self.figures)


def _compact(text: str) -> str:
    return "".join(text.split())


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _union(boxes: Sequence[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _gbox(g: Graphic) -> BBox:
    return (g.x0, g.top, g.x1, g.bottom)


def _wbox(w: Word) -> BBox:
    return (w.x0, w.top, w.x1, w.bottom)


def _center_in(w: Word, b: BBox) -> bool:
    cx = (w.x0 + w.x1) / 2
    return b[0] <= cx <= b[2] and b[1] <= w.mid_y <= b[3]


def _contains(outer: BBox, inner: BBox) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _overlap_share(a: BBox, b: BBox) -> float:
    inter = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return _area(inter) / _area(a) if _area(a) else 0.0


def _in_bands(b: BBox, height: float) -> bool:
    return b[3] <= TOP_BAND * height or b[1] >= (1 - BOTTOM_BAND) * height


def _is_rule(g: Graphic) -> bool:
    return g.kind in ("line", "rect") and min(g.width, g.height) <= RULE_MAX_THICKNESS_PT


def _cluster(items: Sequence[Graphic]) -> list[list[Graphic]]:
    """外扩 VECTOR_JOIN_PT 后外接框相交即连通（按 top 排序的扫描线 + 并查集）。"""
    order = sorted(range(len(items)), key=lambda i: items[i].top)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for pos, i in enumerate(order):
        gi = items[i]
        for j in order[pos + 1 :]:
            gj = items[j]
            if gj.top > gi.bottom + VECTOR_JOIN_PT:
                break
            if gj.x0 <= gi.x1 + VECTOR_JOIN_PT and gi.x0 <= gj.x1 + VECTOR_JOIN_PT:
                parent[find(j)] = find(i)
    groups: dict[int, list[Graphic]] = {}
    for i, g in enumerate(items):
        groups.setdefault(find(i), []).append(g)
    return list(groups.values())


def _marks(cluster: Sequence[Graphic], box: BBox) -> tuple[int, int, int]:
    """(柱, 曲线 / 折线, 斜线)。大面积填充（底色面板）不算标记。"""
    bg = BACKGROUND_AREA_SHARE * _area(box)
    bars = sum(
        1
        for g in cluster
        if g.kind == "rect" and g.filled and not _is_rule(g) and g.width * g.height < bg
    )
    curves = sum(
        1
        for g in cluster
        if g.kind == "curve"
        and g.points >= CURVE_MIN_POINTS
        and not (g.filled and g.width * g.height >= bg)
    )
    diagonals = sum(
        1 for g in cluster if g.kind == "line" and min(g.width, g.height) > RULE_MAX_THICKNESS_PT
    )
    return bars, curves, diagonals


def embedded_image_boxes(graphics: Sequence[Graphic], width: float, height: float) -> list[BBox]:
    page_area = width * height
    out: list[BBox] = []
    for g in graphics:
        if g.kind != "image" or g.width <= 0 or g.height <= 0:
            continue
        share = g.width * g.height / page_area
        if share < IMG_MIN_AREA_SHARE or min(g.width, g.height) < IMG_MIN_SIDE_PT:
            continue  # 图标
        if share > IMG_MAX_AREA_SHARE:
            continue  # 整页底图 / 水印
        if _in_bands(_gbox(g), height) and share < BAND_MAX_AREA_SHARE:
            continue  # 页眉页脚 logo
        aspect = g.width / g.height
        if (
            max(g.width, g.height) <= SQUARE_MAX_SIDE_PT
            and SQUARE_ASPECT[0] <= aspect <= SQUARE_ASPECT[1]
        ):
            continue  # 二维码 / 方形 logo
        out.append(_gbox(g))
    return out


def grid_boxes(
    graphics: Sequence[Graphic], rows: Sequence[Sequence[Word]], height: float
) -> list[BBox]:
    """表格网格：只由细线组成、贯通横线 >= 3 条且贯通竖线 >= 2 条、
    框内无绘图标记、框内 >= 2 行多段文字。"""
    rules = [g for g in graphics if _is_rule(g) and not _in_bands(_gbox(g), height)]
    marks = [g for g in graphics if g.kind != "image" and not _is_rule(g)]
    out: list[BBox] = []
    for c in _cluster(rules):
        box = _union([_gbox(g) for g in c])
        w, h = box[2] - box[0], box[3] - box[1]
        hs = {round(g.top) for g in c if g.width >= GRID_SPAN_SHARE * w and g.width > g.height}
        vs = {round(g.x0) for g in c if g.height >= GRID_SPAN_SHARE * h and g.height > g.width}
        if len(hs) < GRID_MIN_H_RULES or len(vs) < GRID_MIN_V_RULES:
            continue
        if any(_contains(box, _gbox(m)) and _marks([m], box) != (0, 0, 0) for m in marks):
            continue  # 框内有柱 / 线：这是带边框的图表，不是表格
        inside = [[w_ for w_ in r if _center_in(w_, box)] for r in rows]
        if sum(1 for r in inside if len(split_segments(r, FRAGMENT_SEGMENT_GAP)) >= 2) >= 2:
            out.append(box)
    return out


def plot_boxes(
    graphics: Sequence[Graphic], width: float, height: float, grids: Sequence[BBox]
) -> list[BBox]:
    elems = [
        g
        for g in graphics
        if g.kind != "image"
        and not _in_bands(_gbox(g), height)
        and not any(_contains(gb, _gbox(g)) for gb in grids)
    ]
    out: list[BBox] = []
    for c in _cluster(elems):
        box = _union([_gbox(g) for g in c])
        if len(c) < VECTOR_MIN_ELEMENTS or _area(box) < VECTOR_MIN_AREA_SHARE * width * height:
            continue
        bars, curves, diagonals = _marks(c, box)
        if bars >= MIN_BARS or curves >= 1 or diagonals >= MIN_DIAGONALS:
            out.append(box)
    return out


def _numeric_text(text: str) -> bool:
    t = _compact(text)
    return bool(t) and 2 * sum(ch in _NUMERICISH for ch in t) >= len(t)


def is_sentence_shaped(text: str) -> bool:
    """句子 / 列表项 / 脚注：不会是图表刻度或数据标签。"""
    t = text.strip()
    return bool(_LIST_OR_NOTE.match(t) or _SENTENCE_END.search(t) or _CJK_COMMA.search(t))


def _label_number_row(row: Sequence[Word]) -> bool:
    """一行里既有文字词又有数字词（营业收入 91.2）：表格行的形状；图表刻度 / 数据标签全是数字。"""
    return (
        len(row) >= 2
        and any(not _numeric_text(w.text) for w in row)
        and any(_numeric_text(w.text) for w in row)
    )


def _at_body_size(words: Sequence[Word], body_size: float) -> bool:
    return body_size > 0 and max(w.size for w in words) >= BODY_SIZE_SHARE * body_size


def _below_body_size(words: Sequence[Word], body_size: float) -> bool:
    """字号未知（0）按"小于正文"处理，保持无字号信息的夹具的旧行为。"""
    sizes = [w.size for w in words]
    return body_size <= 0 or min(sizes) <= 0 or max(sizes) < BODY_SIZE_SHARE * body_size


def _is_band_label(seg: Sequence[Word], body_size: float) -> bool:
    text = _row_text(seg)
    if is_sentence_shaped(text):
        return False
    return _numeric_text(text) or _below_body_size(seg, body_size)


def is_caption_text(text: str) -> bool:
    return bool(_CAPTION.match(text.strip()))


def is_source_note(text: str) -> bool:
    return bool(_SOURCE_NOTE.match(text.strip()))


def _row_text(row: Sequence[Word]) -> str:
    return " ".join(w.text for w in row)


def is_fragment_row(row: Sequence[Word]) -> bool:
    """整行很短且以数字类字符为主（"40%" "( 4 %" ")"），
    或一行里 >= 3 段短词且过半含数字（刻度行）。"""
    text = _compact("".join(w.text for w in row))
    if not text:
        return False
    if len(text) <= FRAGMENT_MAX_CHARS and sum(
        ch in _NUMERICISH for ch in text
    ) >= FRAGMENT_NUMERIC_SHARE * len(text):
        return True
    segs = [_compact("".join(w.text for w in s)) for s in split_segments(row, FRAGMENT_SEGMENT_GAP)]
    if len(segs) < 3:
        return False
    return all(len(s) <= FRAGMENT_MAX_CHARS for s in segs) and 2 * sum(
        any(ch.isdigit() for ch in s) for s in segs
    ) >= len(segs)


def residual_fragment_rows(rows: Sequence[Sequence[Word]]) -> int:
    """剥离后仍留在正文里的碎片行数。

    >= RESIDUAL_MIN_FRAGMENT_ROWS 说明有图表没检出（T5 判 chart_unresolved）。"""
    return sum(1 for r in rows if is_fragment_row(r))


def _is_label_row(row: Sequence[Word]) -> bool:
    return all(
        len(_compact("".join(w.text for w in s))) <= LABEL_MAX_CHARS
        for s in split_segments(row, FRAGMENT_SEGMENT_GAP)
    )


def _nearest(rows: Sequence[Sequence[Word]], box: BBox, gap: float, below: bool) -> str:
    """rows 可以是整行，也可以是行内的一段（图注 / 来源按段匹配，见 strip_charts）。"""
    best: tuple[float, str] | None = None
    for r in rows:
        x0, x1 = min(w.x0 for w in r), max(w.x1 for w in r)
        top, bottom = min(w.top for w in r), max(w.bottom for w in r)
        if x1 < box[0] or x0 > box[2]:
            continue
        d = top - box[3] if below else box[1] - bottom
        if -2.0 <= d <= gap and (best is None or d < best[0]):
            best = (d, _row_text(r))
    return best[1] if best else ""


def strip_charts(layout: PageLayout, body_size: float) -> ChartStrip:
    em = body_size or 10.0
    words = layout.words
    upright_rows = cluster_rows([w for w in words if w.upright])
    # 图注 / 来源按"段"匹配：图注与左栏正文同基线时整行文字不以"图"开头（T4 评审 I2）
    segments = [seg for r in upright_rows for seg in split_segments(r, FRAGMENT_SEGMENT_GAP)]
    caption_rows = [g for g in segments if is_caption_text(_row_text(g))]
    source_rows = [g for g in segments if is_source_note(_row_text(g))]
    keep = {id(w) for r in (*caption_rows, *source_rows) for w in r}
    grids = grid_boxes(layout.graphics, upright_rows, layout.height)
    candidates: list[tuple[BBox, Detection]] = []
    candidates.extend(
        (b, "embedded_image")
        for b in embedded_image_boxes(layout.graphics, layout.width, layout.height)
    )
    candidates.extend(
        (b, "vector") for b in plot_boxes(layout.graphics, layout.width, layout.height, grids)
    )
    protected = set(keep) | {id(w) for w in words for g in grids if _center_in(w, g)}
    table_regions = find_table_regions(upright_rows)
    for reg in table_regions:
        protected |= {id(w) for r in reg for w in r}
    taken: set[int] = set()
    accepted: list[BBox] = []
    figures: list[FigureRegion] = []
    discarded: list[str] = []
    unsafe: str | None = None
    for box, detection in candidates:
        if any(_overlap_share(box, a) >= DEDUP_OVERLAP for a in accepted):
            continue
        inner = [w for w in words if _center_in(w, box) and id(w) not in taken]
        tag = f"{detection} {tuple(round(v) for v in box)}"
        # 表格区域横跨绘图框边缘：>= 2 行同时有框内的文字词和框外的词。框内只有数字词的行是图表自己的
        # 数据标签（图表紧邻表格时 cluster_rows 把它们与表格行聚到同一行）。T4 评审 C1：此前"落在候选框内
        # 的表格词不受保护"的豁免让原来的守卫形同虚设
        if any(
            sum(
                1
                for r in reg
                if any(_center_in(w, box) and not _numeric_text(w.text) for w in r)
                and any(not _center_in(w, box) for w in r)
            )
            >= 2
            for reg in table_regions
        ):
            discarded.append(f"{tag}：表格区域横跨绘图框边缘，放弃（不剥任何字）")
            continue
        inner_rows = cluster_rows([w for w in inner if w.upright and id(w) not in keep])
        if (
            sum(1 for r in inner_rows if len(split_segments(r, FRAGMENT_SEGMENT_GAP)) >= 2)
            >= TABLE_LIKE_MIN_ROWS
        ):
            discarded.append(f"{tag}：绘图框内是对齐的文字列（表格），放弃")
            continue
        if sum(1 for r in inner_rows if _label_number_row(r)) >= LABEL_NUMBER_MIN_ROWS:
            discarded.append(f"{tag}：绘图框内有 >= 2 行「文字 + 数字」（表格形状），放弃")
            continue
        if not all(_is_label_row(r) for r in inner_rows):
            discarded.append(f"{tag}：绘图框内有正文行，不是图表")
            continue
        # 句子形状的行留在正文；嵌入图片上与正文同字号及以上的文字也留在正文（T4 评审 C3）
        inner_keep = {
            id(w)
            for r in inner_rows
            for seg in split_segments(r, FRAGMENT_SEGMENT_GAP)
            if is_sentence_shaped(_row_text(seg))
            or (detection == "embedded_image" and _at_body_size(seg, body_size))
            for w in seg
        }
        inner = [w for w in inner if id(w) not in inner_keep]
        m = LABEL_MARGIN_EM * em
        outer = (box[0] - m, box[1] - m, box[2] + m, box[3] + m)
        labels: list[Word] = []
        for r in upright_rows:
            outside = [w for w in r if _center_in(w, outer) and not _center_in(w, box)]
            segs = split_segments(outside, FRAGMENT_SEGMENT_GAP)
            # 紧贴图表的表格行（文字 + 数字，或 >= 2 段正文字号）不是图表标签（T4 评审 C2）
            if (
                _label_number_row(outside)
                or sum(1 for g in segs if _at_body_size(g, body_size)) >= 2
            ):
                continue
            for seg in segs:
                if (
                    len(_compact("".join(w.text for w in seg))) <= LABEL_MAX_CHARS
                    and _is_band_label(seg, body_size)
                    and not any(id(w) in protected or id(w) in taken for w in seg)
                ):
                    labels.extend(seg)
        labels += [
            w
            for w in words
            if not w.upright
            and _center_in(w, outer)
            and not _center_in(w, box)
            and id(w) not in taken
            and len(_compact(w.text)) <= LABEL_MAX_CHARS
        ]
        strip = [w for w in [*inner, *labels] if id(w) not in keep]
        if any(not w.upright and any(ch.isdigit() for ch in w.text) for w in strip):
            unsafe = REASON_FIGURE_ROTATED
        taken |= {id(w) for w in strip}
        region = _union([box, *(_wbox(w) for w in strip)])
        gap = CAPTION_MAX_GAP_EM * em
        caption = _nearest(caption_rows, region, gap, below=False) or _nearest(
            caption_rows, region, gap, below=True
        )
        figures.append(
            FigureRegion(
                layout.page,
                region,
                detection,
                caption,
                _nearest(source_rows, region, gap, below=True),
                len(strip),
            )
        )
        accepted.append(box)
    if not taken:
        return ChartStrip(layout, tuple(figures), (), tuple(discarded), unsafe)
    gone = [w for w in words if id(w) in taken]
    items = tuple(
        (REASON_FIGURE, _row_text(r)) for r in cluster_rows([w for w in gone if w.upright])
    )
    items += tuple((REASON_FIGURE, w.text) for w in gone if not w.upright)
    kept = [w for w in words if id(w) not in taken]
    return ChartStrip(
        replace(layout, words=kept, text=body_text(kept)),
        tuple(figures),
        items,
        tuple(discarded),
        unsafe,
    )
