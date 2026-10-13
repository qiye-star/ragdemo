"""确定性排版（纯函数，不用大模型）：把版面提取得到的词坐标排成 markdown。

溯源是核心约束：输出的每个字符都直接来自 PDF 文字层——只做四件事：
  1. 给标题行的行首加 `#`（不新增任何文字）。标题有三类：字号标题；编号标题（第X章 / 第X节 /
     一、/ （一）/ 1. / 1.1 / （1） 与关键词标题，需版式信号并经同一文档内的编号连续性校验，
     按编号体系定级，见 headings.py）；仅粗体的短行（全文粗体占比过高时关闭）；
  2. 编号标题行内有大空隙时（标题 + 行尾数值），在空隙处拆成两个块（只插入换行，不增删字符）；
  3. 把同一段的相邻行合并（只决定行间要不要空格）；
  4. 在表格所在位置原样插入由坐标还原的表格 markdown。
不做任何改写：列表标记原样保留，不换成 `-`；没有生成文字，也就不可能凭空多出或丢失字符。

多栏版面的阅读顺序无法靠单栏规则确定，直接返回 `layout_unsupported` 交给上层降级。

方向安全：含旋转 / 竖排文字、带 /Rotate、或文本矩阵从右向左的页一律 `layout_unsupported`
（pdfplumber 会把这类文字的字序倒过来，数字变成 "12.5" → "5.21"，而自检基准同样是倒的）。

Known limitations（v1 只用于实验，不得用于实验以外的场景）：
- 两栏基线错位（v1 限制 I1）已由 columns.detect_columns 的整页纵向空白带处理。
- 研报首页的侧栏 + 图表页同理可能产出 ok 但顺序错乱的文字。"""

from __future__ import annotations

import itertools
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from ragdemo.parse.local.columns import detect_columns
from ragdemo.parse.local.headings import (
    MAX_LEVEL,
    Candidate,
    Numbering,
    assign_levels,
    keep_consistent,
    numbering_of,
)
from ragdemo.parse.local.layout import PageLayout, Word, unsafe_orientation_reason
from ragdemo.parse.local.number_closure import fold_text
from ragdemo.parse.local.tables import (
    cluster_rows,
    column_edges,
    find_table_regions,
    grid_to_markdown,
    rows_to_grid,
    split_segments,
)

# ---- 命名常量 ----
SIZE_BUCKET = 0.5  # 字号按 0.5pt 取整后统计
HEADING_SIZE_RATIO = 1.15  # 整行字号 >= 正文 * 此值 才可能是标题
HEADING_MAX_CHARS = 60
BOLD_HEADING_MAX_CHARS = 20
NUMBERED_HEADING_MAX_CHARS = 40  # 编号标题（拆行后的第一段）最长字符数
HEADING_GAP_FACTOR = 1.2  # 编号标题的版式信号：上或下行距 > 正文行距 * 此值
HEADING_EMPHASIS_RATIO = 1.05  # 编号标题的版式信号：字号 >= 正文 * 此值
NUMBERED_NEEDS_SIGNAL = True  # 编号标题必须至少有一个版式信号（行距 / 粗体 / 字号）
HEADING_SPLIT_GAP_EM = 2.0  # 行内 >= 此值 * 字号 的空隙把"编号标题 + 行尾数值"拆成两行
BOLD_DOC_MAX_SHARE = 0.15  # 全文粗体字符占比超过此值 → 粗体是强调手法，关闭"仅粗体"标题规则
MIN_BOLD_LEVEL = 3
BOLD_HEADING_GAP_FACTOR = 1.3  # 粗体标题：上下行距须大于正文行距的这个倍数
MAX_HEADING_LEVEL = 3
PARAGRAPH_GAP_FACTOR = 1.6  # 行距 > 正文行距 * 此值 → 另起一段
INDENT_FACTOR = 1.2  # 首行缩进 >= 字号 * 此值 → 另起一段
SHORT_LINE_FACTOR = 3.0  # 上一行右端比版心右缘短这么多个字宽，且以句末标点收尾 → 另起一段
PITCH_CAP_FACTOR = 2.0  # 统计正文行距时，忽略大于字号 * 此值的行间距（段间距、标题间距）
FALLBACK_PITCH_FACTOR = 1.3  # 文档里凑不出行距样本时，行距 = 字号 * 此值

SENTENCE_END = "。；，、：！？.;,:!?"
LIST_MARKER = re.compile(
    r"^(?:\d{1,3}[.、．)）](?!\d)"  # 1.  1、  1)
    r"|[（(]\d{1,3}[）)]"  # （1）  (2)
    r"|[一二三四五六七八九十]{1,3}[、．.]"  # 一、
    r"|[（(][一二三四五六七八九十]{1,3}[）)]"  # （一）
    r"|[•·●■◆▪○◦▶►※①②③④⑤⑥⑦⑧⑨⑩\ue000-\uf8ff]"  # 含符号字体圆点（私有区）
    r"|[-–—*]\s)"
)

Status = Literal["ok", "layout_unsupported"]


@dataclass(frozen=True)
class TypesetResult:
    status: Status
    markdown: str
    reason: str = ""
    tables: list[str] = field(default_factory=list)  # 本页还原出的表格 markdown（自检用）


@dataclass
class _Line:
    text: str
    size: float  # 整行最小字号（判标题用）
    bold: bool
    x0: float
    x1: float
    mid: float
    head_text: str  # 按 HEADING_SPLIT_GAP_EM 切出的第一段；整行一段时等于 text
    head_x1: float
    rest_text: str  # 其余段（无则空串）
    heading_level: int = 0
    split: bool = False  # 标题只取第一段，其余段另起一段（只插入换行）


@dataclass
class _Table:
    markdown: str
    first_mid: float
    last_mid: float


_Item = _Line | _Table


@dataclass
class _PageItems:
    items: list[_Item]
    orientation_reason: str | None  # 旋转 / 倒向文字：不排版
    multicolumn: bool
    column_reason: str = ""


# ---- 文本拼接 ----
_CJK_COMPATIBLE_PUNCT = "“”‘’—…·"  # 东亚宽度为 Ambiguous，但在中文排版里和汉字连排


_NUMBER_SYNTAX = "-.%()"


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F") or ch in _CJK_COMPATIBLE_PUNCT


def _no_space_between(a: str, b: str) -> bool:
    """两侧都是中文字符（含全角标点）时不加空格；其余（拉丁、数字与中文相邻）保留一个空格。

    任一侧折叠（全角 → 半角）后是数字或数字语法字符 `- . % ( )`
    （逗号：仅紧邻数字时）时一律保留空格：
    全角的 `－`、`５`、`．` 在 _wide 里算宽字符，丢掉空格会让 `－ ５．０` 变成 `－５．０`，
    改变数字的符号（多重集合检查看不出来）。"""
    fa, fb = fold_text(a), fold_text(b)
    if any(c.isdigit() or c in _NUMBER_SYNTAX for c in (fa, fb)):
        return False
    if (fa == "," and fb.isdigit()) or (fb == "," and fa.isdigit()):
        return False  # 逗号只有紧邻数字才是数字语法（千分位）；中文句中的逗号照常连排
    return _wide(a) and _wide(b)


def _join_words(words: Sequence[Word]) -> str:
    out = ""
    for w in words:
        if out and not _no_space_between(out[-1], w.text[0]):
            out += " "
        out += w.text
    return out


def _join_lines(prev: str, nxt: str) -> str:
    a, b = prev[-1], nxt[0]
    if _no_space_between(a, b) or (a == "-" and b.islower()):
        return prev + nxt
    return prev + " " + nxt


# ---- 文档统计 ----
def _bucket(size: float) -> float:
    return round(size / SIZE_BUCKET) * SIZE_BUCKET


def body_font_size(pages: Sequence[PageLayout]) -> float:
    """整份文档按字符数加权的字号众数（取整到 0.5pt）；没有字号信息返回 0.0。"""
    counts: Counter[float] = Counter()
    for p in pages:
        for w in p.words:
            if w.upright and w.size > 0:
                counts[_bucket(w.size)] += len(w.text)
    if not counts:
        return 0.0
    top = max(counts.values())
    return min(s for s, c in counts.items() if c == top)  # 并列取较小：正文宁小勿大


def _line_size(words: Sequence[Word]) -> float:
    return min((w.size for w in words), default=0.0)


def _page_items(layout: PageLayout, body_size: float) -> _PageItems:
    rows = cluster_rows([w for w in layout.words if w.upright])
    regions = find_table_regions(rows)
    region_of = {id(w): k for k, reg in enumerate(regions) for r in reg for w in r}
    items: list[_Item] = []
    plain_rows: list[list[Word]] = []
    emitted: set[int] = set()
    for row in rows:
        k = region_of.get(id(row[0]))  # 表格区域由整行组成：行首词在区域里，整行都在
        if k is not None:
            if k not in emitted:
                emitted.add(k)
                reg = regions[k]
                mids = [sum(w.mid_y for w in r) / len(r) for r in reg]
                md = grid_to_markdown(rows_to_grid(reg, column_edges(reg)))
                items.append(_Table(md, mids[0], mids[-1]))
            continue
        plain_rows.append(row)
        size = _line_size(row)
        segs = split_segments(row, HEADING_SPLIT_GAP_EM * (size or body_size or 10.0))
        head = segs[0]
        rest = [w for seg in segs[1:] for w in seg]
        items.append(
            _Line(
                text=_join_words(row),
                size=size,
                bold=all(w.bold for w in row),
                x0=row[0].x0,
                x1=max(w.x1 for w in row),
                mid=sum(w.mid_y for w in row) / len(row),
                head_text=_join_words(head),
                head_x1=max(w.x1 for w in head),
                rest_text=_join_words(rest) if rest else "",
            )
        )
    verdict = detect_columns(plain_rows, layout.width, body_size)
    return _PageItems(items, unsafe_orientation_reason(layout), verdict.multicolumn, verdict.reason)


def _body_pitch(all_items: Sequence[_PageItems], body_size: float) -> float:
    pitches: list[float] = []
    for pi in all_items:
        for a, b in itertools.pairwise(pi.items):
            if isinstance(a, _Line) and isinstance(b, _Line):
                gap = b.mid - a.mid
                if (
                    abs(a.size - body_size) <= SIZE_BUCKET
                    and abs(b.size - body_size) <= SIZE_BUCKET
                    and 0 < gap <= PITCH_CAP_FACTOR * body_size
                ):
                    pitches.append(gap)
    if pitches:
        return statistics.median(pitches)
    return FALLBACK_PITCH_FACTOR * (body_size or 10.0)


def _heading_candidate(line: _Line, body_size: float) -> bool:
    text = line.text.strip()
    return (
        body_size > 0
        and line.size >= HEADING_SIZE_RATIO * body_size
        and len(text) <= HEADING_MAX_CHARS
        and text[-1] not in SENTENCE_END
        and any(ch.isalpha() for ch in text)
    )


def _gaps(items: Sequence[_Item], i: int) -> tuple[float | None, float | None]:
    line = items[i]
    assert isinstance(line, _Line)
    above = below = None
    if i > 0:
        prev = items[i - 1]
        above = line.mid - (prev.mid if isinstance(prev, _Line) else prev.last_mid)
    if i + 1 < len(items):
        nxt = items[i + 1]
        below = (nxt.mid if isinstance(nxt, _Line) else nxt.first_mid) - line.mid
    return above, below


def _right_margin(items: Sequence[_Item]) -> float:
    xs = sorted(it.x1 for it in items if isinstance(it, _Line))
    return xs[int(0.9 * (len(xs) - 1))] if xs else 0.0


def _numbered(
    items: Sequence[_Item], i: int, body_size: float, pitch: float, right: float
) -> Numbering | None:
    line = items[i]
    assert isinstance(line, _Line)
    head = line.head_text.strip()
    n = numbering_of(head)
    if n is None or len(head) > NUMBERED_HEADING_MAX_CHARS:
        return None
    if n.system != "keyword" and head[-1] in SENTENCE_END:
        return None
    size = line.size or body_size or 10.0
    if not line.rest_text and line.head_x1 >= right - SHORT_LINE_FACTOR * size:
        return None  # 一直写到版心右缘：折行的正文段落，不是标题
    if not NUMBERED_NEEDS_SIGNAL:
        return n
    above, below = _gaps(items, i)
    signal = (
        line.bold
        or (body_size > 0 and line.size >= HEADING_EMPHASIS_RATIO * body_size)
        or (above is not None and above > HEADING_GAP_FACTOR * pitch)
        or (below is not None and below > HEADING_GAP_FACTOR * pitch)
    )
    return n if signal else None


def _bold_heading(items: Sequence[_Item], i: int, body_size: float, pitch: float) -> bool:
    line = items[i]
    assert isinstance(line, _Line)
    text = line.text.strip()
    if not (
        line.bold
        and len(text) <= BOLD_HEADING_MAX_CHARS
        and text[-1] not in SENTENCE_END
        and any(ch.isalpha() for ch in text)
        and (body_size == 0 or line.size <= HEADING_SIZE_RATIO * body_size)
    ):
        return False
    for j in (i - 1, i + 1):  # 相邻行也是整行粗体：强调段落，不是标题
        if 0 <= j < len(items):
            nb = items[j]
            if isinstance(nb, _Line) and nb.bold:
                return False
    above, below = _gaps(items, i)
    gaps = [g for g in (above, below) if g is not None]
    return bool(gaps) and all(g > BOLD_HEADING_GAP_FACTOR * pitch for g in gaps)


def _mark_headings(
    per_page: Sequence[_PageItems], body: float, pitch: float, size_levels: dict[float, int]
) -> None:
    """整份文档统一定标题：字号标题 → 编号标题（连续性校验 + 按体系定级）→ 仅粗体标题。"""
    live = [pi for pi in per_page if not pi.multicolumn and pi.orientation_reason is None]
    total = bold = 0
    for pi in live:
        for it in pi.items:
            if isinstance(it, _Line):
                n = len(it.text.replace(" ", ""))
                total += n
                bold += n if it.bold else 0
    cands: list[Candidate] = []
    flat: list[_Line] = []
    for pi in live:
        right = _right_margin(pi.items)
        for i, it in enumerate(pi.items):
            if not isinstance(it, _Line):
                continue
            if _heading_candidate(it, body):
                it.heading_level = size_levels.get(_bucket(it.size), MAX_HEADING_LEVEL)
                continue
            num = _numbered(pi.items, i, body, pitch, right)
            if num is not None:
                cands.append(Candidate(len(flat), num))
                flat.append(it)
    kept = keep_consistent(cands)
    base = max(
        (it.heading_level for pi in live for it in pi.items if isinstance(it, _Line)), default=0
    )
    levels = assign_levels([c.numbering.system for c in cands if c.index in kept], base=base)
    deepest = base
    for c in cands:
        if c.index in kept:
            ln = flat[c.index]
            ln.heading_level = levels[c.numbering.system]
            ln.split = bool(ln.rest_text)
            deepest = max(deepest, ln.heading_level)
    if total == 0 or bold > BOLD_DOC_MAX_SHARE * total:
        return
    bold_level = min(max(MIN_BOLD_LEVEL, deepest + 1), MAX_LEVEL)
    for pi in live:
        for i, it in enumerate(pi.items):
            if (
                isinstance(it, _Line)
                and not it.heading_level
                and _bold_heading(pi.items, i, body, pitch)
            ):
                it.heading_level = bold_level


# ---- 渲染 ----
def _render(pi: _PageItems, pitch: float) -> str:
    items = pi.items
    lines = [it for it in items if isinstance(it, _Line) and not it.heading_level]
    margin = 0.0
    right = 0.0
    if lines:
        x0s = Counter(round(ln.x0) for ln in lines)
        top = max(x0s.values())
        margin = float(min(x for x, c in x0s.items() if c == top))
        right = sorted(ln.x1 for ln in lines)[int(0.9 * (len(lines) - 1))]

    blocks: list[str] = []
    para: str | None = None
    para_last: _Line | None = None
    para_is_list = False

    def flush() -> None:
        nonlocal para, para_last, para_is_list
        if para is not None:
            blocks.append(para)
        para, para_last, para_is_list = None, None, False

    for it in items:
        if isinstance(it, _Table):
            flush()
            blocks.append(it.markdown)
            continue
        if it.heading_level:  # 标题行只在行首加 #；拆行时其余段另起一段（只插入换行）
            flush()
            if it.split:
                blocks.append("#" * it.heading_level + " " + it.head_text)
                blocks.append(it.rest_text)  # 行尾数值自成一块，不与后文合并（复核裁决 I1）
            else:
                blocks.append("#" * it.heading_level + " " + it.text)
            continue
        if para is None or para_last is None:
            para, para_last, para_is_list = it.text, it, bool(LIST_MARKER.match(it.text))
            continue
        prev = para_last
        new_para = (
            it.mid - prev.mid > PARAGRAPH_GAP_FACTOR * pitch
            or abs(it.size - prev.size) > SIZE_BUCKET
            or bool(LIST_MARKER.match(it.text))
            or (
                not para_is_list
                and it.x0 - margin >= INDENT_FACTOR * it.size
                and it.x0 - prev.x0 >= INDENT_FACTOR * it.size
            )
            or (prev.text[-1] in SENTENCE_END and prev.x1 < right - SHORT_LINE_FACTOR * prev.size)
        )
        if new_para:
            blocks.append(para)
            para, para_last, para_is_list = it.text, it, bool(LIST_MARKER.match(it.text))
        else:
            para = _join_lines(para, it.text)
            para_last = it
    flush()
    return "\n\n".join(blocks)


def typeset_document(pages: Sequence[PageLayout]) -> list[TypesetResult]:
    """整份文档排版：正文字号与标题级别按整份文档统计，逐页返回结果（与输入等长）。"""
    body = body_font_size(pages)
    per_page = [_page_items(p, body) for p in pages]
    pitch = _body_pitch(per_page, body)
    sizes = sorted(
        {
            _bucket(it.size)
            for pi in per_page
            if not pi.multicolumn and pi.orientation_reason is None
            for it in pi.items
            if isinstance(it, _Line) and _heading_candidate(it, body)
        },
        reverse=True,
    )
    levels = {s: min(i + 1, MAX_HEADING_LEVEL) for i, s in enumerate(sizes)}
    _mark_headings(per_page, body, pitch, levels)
    out: list[TypesetResult] = []
    for pi in per_page:
        if pi.orientation_reason is not None:
            out.append(TypesetResult("layout_unsupported", "", pi.orientation_reason))
            continue
        if pi.multicolumn:
            out.append(TypesetResult("layout_unsupported", "", pi.column_reason))
            continue
        tables = [it.markdown for it in pi.items if isinstance(it, _Table)]
        out.append(TypesetResult("ok", _render(pi, pitch), "", tables))
    return out
