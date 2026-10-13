"""确定性 L0（第二版）：只接受单栏、无表格、非财务报表的有文字层页。

文档级入口：正文字号、行距、标题字号层级、编号连续性都按整份文档统计（所以它不是 PageEngine——
那是"单页 PDF → markdown"的协议）。次序：剥装饰 → 识别并剥离图表区域（chart.strip_charts；图表
只存图 + 元数据，区域内文字无损记入 stripped_text）→ 判页型 → 只有 plain_text 进入排版与自检。
其余页带着页型原因交给上层（分级路由的 L1 / L3）。自检就是 pipeline.self_check（双向数字闭合 +
双向字符闭合 + 以原始 extract_text 为基准的顺序检查），不做任何豁免；任何不通过都说明排版器有 bug，
记 closure_failed 并降级。表格页永远不在这里排版（L0 的坐标表格还原不被信任用来存表）。

页型判定次序（控制者裁决 U-D3，2026-10-13）：no_text_layer → financial_statement → 表格（坐标表格 /
疑似表格行）→ multicolumn → chart_unresolved → plain_text。表格与多栏同属"拒收"，次序只改变标签、
不改变去向。计划原文的次序（多栏在表格之前）保留为 ``multicolumn_first=True``，
只供 G5 / G11 并排报告
旧口径；旋转文字残留的两处检查（版面方向不可信）在两种次序下都最先判，因为行聚类以方向可信为前提。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ragdemo.parse.figures import FigureRegion
from ragdemo.parse.local.chart import (
    REASON_CHART_UNRESOLVED,
    RESIDUAL_MIN_FRAGMENT_ROWS,
    ChartStrip,
    residual_fragment_rows,
    strip_charts,
)
from ragdemo.parse.local.columns import detect_columns
from ragdemo.parse.local.decor import StrippedPage, strip_decor
from ragdemo.parse.local.layout import PageLayout, Word, extract_pages, unsafe_orientation_reason
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.pipeline import PageResult, needs_handback, order_violation, self_check
from ragdemo.parse.local.tables import cluster_rows, find_table_regions, split_segments
from ragdemo.parse.local.typeset import body_font_size, typeset_document
from ragdemo.parse.tiered.classify import is_financial_statement_page

PageKind = Literal[
    "plain_text", "table", "multicolumn", "chart_unresolved", "financial_statement", "no_text_layer"
]
L0_VERSION: Final = "det-v2"
TABLELIKE_GAP_EM = 2.0
TABLELIKE_MIN_ROWS = 2
REASON_ROTATED: Final = "多栏版面：旋转文字残留"


@dataclass(frozen=True)
class PageClass:
    kind: PageKind
    reason: str = ""


@dataclass(frozen=True)
class L0Page:
    page: int
    kind: PageKind
    result: PageResult
    figures: tuple[FigureRegion, ...] = ()  # 本页检出的图表区域（非接受页也给出）
    body: PageLayout | None = None  # 剥离装饰与图表文字后的正文版面（审计用）
    figure_text: str = ""  # 被剥的图表文字（也已并入 result.stripped_text）

    @property
    def accepted(self) -> bool:
        return self.kind == "plain_text" and self.result.status == "ok"


def tablelike_run(rows: Sequence[Sequence[Word]], body_size: float) -> int:
    """最长的连续"疑似表格行"行数：行内按 >= TABLELIKE_GAP_EM 字宽的空隙切成 >= 2 段，
    且含 >= 2 个数字。"""
    gap = TABLELIKE_GAP_EM * (body_size or 10.0)
    best = cur = 0
    for r in rows:
        if (
            len(split_segments(r, gap)) >= 2
            and sum(numbers_in(" ".join(w.text for w in r)).values()) >= 2
        ):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def classify_page(
    layout: PageLayout,
    *,
    original_text: str,
    body_size: float,
    charts: ChartStrip | None = None,
    multicolumn_first: bool = False,
) -> PageClass:
    """layout：剥离装饰后的版面；original_text：剥离前的文字层（查 cid 乱码用）。先中先得。
    多栏 / 表格 / 残留碎片都在剥离图表文字之后判（图表里的零散标签会造成纵向空白带误报）。
    multicolumn_first=True 是计划原文的旧次序（多栏先于表格），只给 G5 / G11 的并排报告用。"""
    handback = needs_handback(layout.text)
    if handback is None and "(cid:" in original_text:
        handback = "文字层乱码（cid）"
    if handback is not None:
        return PageClass("no_text_layer", handback)
    if is_financial_statement_page(layout.text):
        return PageClass("financial_statement", "财务报表页")
    cs = charts if charts is not None else strip_charts(layout, body_size)
    if cs.unsafe_reason is not None:
        return PageClass("multicolumn", f"{REASON_ROTATED}（{cs.unsafe_reason}）")
    body = cs.layout
    # 修订（Opus 复核 v1 修复轮 2）：w.upright 必须是"由字符文本矩阵判定的水平"
    # （layout.char_is_horizontal：a > 1e-6、|b| <= tan(2°)·a、d > 0），不能信 pdfminer 的
    # upright 标志（它把 -90°~90° 内任意倾斜、带浮点噪声的 90° 矩阵都算直立，数字会以乱序 /
    # 倒序输出且自检看不出）。另须保留 /Rotate 与文本矩阵
    # 从右向左（layout.unsafe_orientation_reason）两项检查，且"被剥离的非水平文字含数字"整页退回。
    if any(not w.upright for w in body.words) or unsafe_orientation_reason(body) is not None:
        return PageClass("multicolumn", REASON_ROTATED)
    rows = cluster_rows([w for w in body.words if w.upright])
    regions = find_table_regions(rows)
    in_table = {id(w) for reg in regions for r in reg for w in r}
    # 裁决 2：残留碎片行与多栏检测都不看表格区域内的行（表格行的短数字单元格不是图表碎片）
    plain = [r for r in rows if id(r[0]) not in in_table]
    cols = detect_columns(plain, body.width, body_size)
    table = PageClass("table", f"坐标表格 {len(regions)} 处") if regions else None
    tablelike = (
        PageClass("table", "疑似表格行")
        if tablelike_run(plain, body_size) >= TABLELIKE_MIN_ROWS
        else None
    )
    multicol = PageClass("multicolumn", cols.reason) if cols.multicolumn else None
    chart = (
        PageClass("chart_unresolved", REASON_CHART_UNRESOLVED)
        if residual_fragment_rows(plain) >= RESIDUAL_MIN_FRAGMENT_ROWS
        else None
    )
    if multicolumn_first:  # 旧次序：多栏 → 坐标表格 → 图表未解析 → 疑似表格行
        order = (multicol, table, chart, tablelike)
    else:  # U-D3：坐标表格 → 疑似表格行 → 多栏 → 图表未解析
        order = (table, tablelike, multicol, chart)
    for verdict in order:
        if verdict is not None:
            return verdict
    return PageClass("plain_text")


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _prepare(
    pages: Sequence[PageLayout],
) -> tuple[list[StrippedPage], list[ChartStrip], float, list[int]]:
    stripped = strip_decor(pages)
    live = [
        i
        for i, (p, s) in enumerate(zip(pages, stripped, strict=True))
        if needs_handback(s.layout.text) is None and "(cid:" not in p.text
    ]
    body_size = body_font_size([stripped[i].layout for i in live])
    live_set = set(live)
    charts = [
        strip_charts(s.layout, body_size) if i in live_set else ChartStrip(s.layout)
        for i, s in enumerate(stripped)
    ]
    return stripped, charts, body_size, live


def page_kinds(pages: Sequence[PageLayout], *, multicolumn_first: bool) -> list[PageKind]:
    """只判页型（不排版）。multicolumn_first=True 给出旧次序的页型，供 G5 / G11 并排报告。"""
    stripped, charts, body_size, _ = _prepare(pages)
    return [
        classify_page(
            s.layout,
            original_text=p.text,
            body_size=body_size,
            charts=c,
            multicolumn_first=multicolumn_first,
        ).kind
        for p, s, c in zip(pages, stripped, charts, strict=True)
    ]


def parse_layouts_l0(pages: Sequence[PageLayout]) -> list[L0Page]:
    stripped, charts, body_size, live = _prepare(pages)
    classes = [
        classify_page(s.layout, original_text=p.text, body_size=body_size, charts=c)
        for p, s, c in zip(pages, stripped, charts, strict=True)
    ]
    to_type = [i for i in live if classes[i].kind == "plain_text" and charts[i].layout.words]
    typeset = dict(zip(to_type, typeset_document([charts[i].layout for i in to_type]), strict=True))
    out: list[L0Page] = []
    for i, (orig, s, cs, c) in enumerate(zip(pages, stripped, charts, classes, strict=True)):
        lay = cs.layout
        kept = _join(s.stripped_text, cs.stripped_text)  # 装饰 + 图表文字：都不进输出、都可审计
        kind = c.kind
        if kind == "no_text_layer":
            res = PageResult(lay.page, "no_text_layer", "", lay.text, [], c.reason, kept)
        elif kind != "plain_text":
            res = PageResult(
                lay.page, "layout_unsupported", "", lay.text, [], f"{kind}：{c.reason}", kept
            )
        elif not lay.words:  # 整页只有图表（图注也没有）：正文为空，仍须过顺序检查
            why = order_violation("", orig.text, kept)
            res = (
                PageResult(lay.page, "closure_failed", "", lay.text, [], why, kept)
                if why
                else PageResult(lay.page, "ok", "", lay.text, [], "", kept)
            )
        else:
            t = typeset[i]
            if t.status != "ok":  # 与 classify_page 用同一个检测器，正常不会走到；防御
                kind = "multicolumn"
                res = PageResult(
                    lay.page,
                    "layout_unsupported",
                    "",
                    lay.text,
                    [],
                    f"multicolumn：{t.reason}",
                    kept,
                )
            else:
                violations, reason = self_check(t.markdown, lay.text, t.tables, orig.text, kept)
                res = (
                    PageResult(lay.page, "closure_failed", "", lay.text, violations, reason, kept)
                    if reason
                    else PageResult(lay.page, "ok", t.markdown, lay.text, [], "", kept)
                )
        out.append(L0Page(lay.page, kind, res, cs.figures, lay, cs.stripped_text))
    return out


def parse_pdf_l0(pdf_bytes: bytes) -> list[L0Page]:
    return parse_layouts_l0(extract_pages(pdf_bytes))
