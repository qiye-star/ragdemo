"""页型普查与定期报告识别（纯函数，无 IO）。

两件事：
1. `is_periodic_report` / `force_whole_document_l3`：按标题 / 文件名识别被标成 announcement 的
   年报 / 半年报 / 季报（裁决 U-D6：与 annual_report 同样整份走 L3）。
2. `classify_pages` / `summarize`：对一批 PDF 页做**普查用**页型分类并汇总占比。

普查口径是**代理**，不是 L0 v2 的 `classify_page`：没有装饰剥离（页眉页脚、页码仍在版面里），
没有图表检测（图表碎片留在正文行里，所以从不产出 chart_unresolved；含图表的页会落在
plain_text，或被零散标签推成 multicolumn / table）。因此 plain_text 只是 L0 能接收页数的近似，
方向不确定（图表页本可进 L0，而图表误检造成的 multicolumn / table 在另一侧抵消）。
判定顺序按裁决 U-D3：no_text_layer → financial_statement → table → multicolumn → plain_text。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from ragdemo.parse.local.columns import detect_columns
from ragdemo.parse.local.layout import PageLayout, Word, unsafe_orientation_reason
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.pipeline import needs_handback
from ragdemo.parse.local.tables import cluster_rows, find_table_regions, split_segments
from ragdemo.parse.local.typeset import body_font_size
from ragdemo.parse.tiered.classify import is_financial_statement_page

# ---------------------------------------------------------------- 定期报告识别

PeriodicKind = Literal["annual", "half_year", "quarterly"]

# 这些类型本来就整份走 L3（路由计划：年报 / 季报整份永远走 L3）。
ALWAYS_L3_DOC_TYPES: Final = frozenset({"annual_report", "quarterly"})

# 半年度先于年度判：「半年度报告」含子串「年度报告」，年度 / 年报 用 (?<!半) 排除。
_PATTERNS: Final[tuple[tuple[PeriodicKind, re.Pattern[str]], ...]] = (
    ("half_year", re.compile(r"半年度(?:财务)?报告|半年报|中期报告")),
    ("annual", re.compile(r"(?<!半)年度(?:财务)?报告|(?<!半)年报")),
    ("quarterly", re.compile(r"季度报告|[一二三四]季报|季报")),
)
# 简称（年报 / 季报 / 半年报）比全称更容易出现在制度、决议里，另行把关（见 is_periodic_report）。
_SHORTHAND = re.compile(r"半年报|[一二三四]季报|季报|(?<!半)年报")

# 明显不是报告本体的标题：制度、决议、通知、预告、更正、核查意见、回复函等。
# 「摘要」不在此列：摘要按保守口径仍算定期报告（见 is_periodic_report）。
_EXCLUDE = re.compile(
    "公告|决议|制度|细则|规则|办法|预告|快报|更正|补充|修订|差错|问询|回复|函|"
    "核查|意见|审计机构|披露时间|召开|会议|说明会|提示|延期|承诺|工作报告|述职"
)

# 简称命中且页数已知小于此值的，视为通知 / 声明而非报告（报告摘要最短也有数页）。
MIN_PAGES_FOR_SHORTHAND = 4


def _kind_in(text: str) -> tuple[PeriodicKind, bool] | None:
    """(类型, 是否仅靠简称命中)；不命中 None。"""
    flat = re.sub(r"\s+", "", text)
    if _EXCLUDE.search(flat):
        return None
    for kind, pat in _PATTERNS:
        m = pat.search(flat)
        if m is not None:
            return kind, _SHORTHAND.fullmatch(m.group(0)) is not None
    return None


def is_periodic_report(
    title: str | None, filename: str | None, page_count: int | None
) -> PeriodicKind | None:
    """按标题 / 文件名识别定期报告（年度 / 半年度 / 季度）；识别不出返回 None。

    规则：
    - 标题与文件名任一命中即可（标题优先）。半年度先判，「半年度报告」不会误成 annual。
    - 摘要（「年度报告摘要」「半年度报告摘要」）**算定期报告**：保守口径，宁可多走 L3，也不让摘要里
      的财务表格被按页路由漏判（摘要实测多为表格 / 图表密集）。
    - 含「公告 / 决议 / 制度 / 预告 / 快报 / 更正 / 问询 / 核查意见 / 召开 / 述职」等词的不算：
      业绩预告、业绩快报、年报更正公告、董事会审议年报的决议、年报信息披露制度都不是报告本体。
    - 「年度财务报告」「半年度财务报告」算（财务报表全文，是报告的组成部分）；
      「审计报告」「内部控制评价报告」「ESG 报告」「募集资金专项报告」不含「年度报告 / 季度报告」连写，不会命中。
    - 仅靠简称（年报 / 半年报 / 一季报 / 三季报）命中、且页数已知小于 MIN_PAGES_FOR_SHORTHAND 的，
      视为通知 / 声明，不算（页数未知则算）。
    """
    for text in (title, filename):
        if not text:
            continue
        hit = _kind_in(text)
        if hit is None:
            continue
        kind, shorthand_only = hit
        if shorthand_only and page_count is not None and page_count < MIN_PAGES_FOR_SHORTHAND:
            continue
        return kind
    return None


def force_whole_document_l3(doc_type: str, title: str | None, filename: str | None) -> bool:
    """整份文档是否强制走 L3（TextIn）：doc_type 为 annual_report / quarterly，
    或被标成其他类型（实测多为 announcement）但标题 / 文件名是定期报告（U-D6）。

    集成点（本任务不接线）：分级路由的文档级入口（TieredParser.parse_for，路由计划 T9）在按
    `doc_types` 判范围之前调用它；命中则直接走 TextIn。文件名取原始 PDF 名（无则 None）。"""
    if doc_type in ALWAYS_L3_DOC_TYPES:
        return True
    return is_periodic_report(title, filename, None) is not None


# ---------------------------------------------------------------- 页型普查

PageKind = Literal["no_text_layer", "financial_statement", "table", "multicolumn", "plain_text"]
PAGE_KINDS: Final[tuple[PageKind, ...]] = (
    "no_text_layer",
    "financial_statement",
    "table",
    "multicolumn",
    "plain_text",
)

# 与 L0 v2 计划的 TABLELIKE_GAP_EM / TABLELIKE_MIN_ROWS 同值
# （l0.tablelike_run 尚未落地，先在此近似；它落地后应改为导入）。
TABLELIKE_GAP_EM: Final = 2.0
TABLELIKE_MIN_ROWS: Final = 2


def _tablelike_run(rows: Sequence[Sequence[Word]], body_size: float) -> int:
    """最长的连续「疑似表格行」：行内按 >= TABLELIKE_GAP_EM 字宽的空隙切成 >= 2 段，
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


def _classify(layout: PageLayout, body_size: float) -> PageKind:
    if needs_handback(layout.text) is not None or "(cid:" in layout.text:
        return "no_text_layer"
    if is_financial_statement_page(layout.text):
        return "financial_statement"
    rows = cluster_rows([w for w in layout.words if w.upright])
    regions = find_table_regions(rows)
    if regions or _tablelike_run(rows, body_size) >= TABLELIKE_MIN_ROWS:
        return "table"
    if any(not w.upright for w in layout.words) or unsafe_orientation_reason(layout) is not None:
        return "multicolumn"
    if detect_columns(rows, layout.width, body_size).multicolumn:
        return "multicolumn"
    return "plain_text"


def classify_pages(layouts: Sequence[PageLayout]) -> list[PageKind]:
    """逐页分类（普查代理口径，见模块说明）。正文字号取整份文档的众数。"""
    body_size = body_font_size(layouts)
    return [_classify(p, body_size) for p in layouts]


# ---------------------------------------------------------------- 汇总

Group = Literal["announcement", "periodic", "research"]
GROUPS: Final[tuple[Group, ...]] = ("announcement", "periodic", "research")
# 分级路由的范围：非定期公告 + 研报（定期报告整份走 L3，不参与按页路由）。
ROUTING_GROUPS: Final[tuple[Group, ...]] = ("announcement", "research")


@dataclass(frozen=True)
class Census:
    docs: Mapping[Group, int] = field(default_factory=dict)
    pages: Mapping[Group, Mapping[PageKind, int]] = field(default_factory=dict)

    def page_count(self, groups: Sequence[Group] = ROUTING_GROUPS) -> int:
        return sum(sum(self.pages.get(g, {}).values()) for g in groups)

    def kind_count(self, kind: PageKind, groups: Sequence[Group] = ROUTING_GROUPS) -> int:
        return sum(self.pages.get(g, {}).get(kind, 0) for g in groups)

    def share(self, kind: PageKind, groups: Sequence[Group] = ROUTING_GROUPS) -> float:
        """kind 页占这些组全部页的比例；无页返回 0.0。"""
        total = self.page_count(groups)
        return self.kind_count(kind, groups) / total if total else 0.0

    def scan_share(self, groups: Sequence[Group] = ROUTING_GROUPS) -> float:
        """路由计划 CostInputs.scan_share：无文字层页 / 全部页。"""
        return self.share("no_text_layer", groups)

    def statement_share(self, groups: Sequence[Group] = ROUTING_GROUPS) -> float:
        """路由计划 CostInputs.statement_share：财务报表页 / **有文字层**页。"""
        text_pages = self.page_count(groups) - self.kind_count("no_text_layer", groups)
        return self.kind_count("financial_statement", groups) / text_pages if text_pages else 0.0


def summarize(kinds_by_doc: Mapping[str, tuple[Group, Sequence[PageKind]]]) -> Census:
    """kinds_by_doc：文档键 -> (分组, 逐页页型)。"""
    docs: Counter[Group] = Counter()
    pages: dict[Group, Counter[PageKind]] = {}
    for group, kinds in kinds_by_doc.values():
        docs[group] += 1
        pages.setdefault(group, Counter()).update(kinds)
    return Census(dict(docs), {g: dict(c) for g, c in pages.items()})
