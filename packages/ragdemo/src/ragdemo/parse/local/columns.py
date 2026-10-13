"""整页多栏 / 侧栏检测（纯函数）。

v1 只看"同一行里的空隙"：侧栏行与正文行很少落在同一行（纵向错开几个点就被聚成两行），
所以研报首页的评级框 / 行情框几乎全部漏检，产出"字符一个不少、顺序错乱"的 ok 页。
这里对页面横向逐点扫描：某个 x 在一段连续的行里都没有被任何词跨过，且这段行里 x 左右两侧
各有 >= MIN_SIDE_ROWS 行文字、空白带足够宽、并落在页面中部区域，就是栏间空白带。
另查"竖排单字列"（研报左侧的"公 司 研 究"）。检出即判多栏，由上层降级，不在本地猜阅读顺序。"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.tables import cluster_rows, find_table_regions

# 空白带左缘 >= 页宽 * 下限、右缘 <= 页宽 * 上限（排除页边的右对齐日期 / 页码）
GUTTER_ZONE = (0.15, 0.85)
MIN_GUTTER_EM = 1.5  # 空白带宽度 >= 此值 * 正文字号
MIN_SIDE_ROWS = 3  # 空白带两侧各至少这么多行
X_STEP = 2.0  # 横向扫描步长（pt）
VERTICAL_MIN_CHARS = 3  # 竖排单字列：同一 x 上至少连续这么多个孤立单字
VERTICAL_X_TOL = 2.0
VERTICAL_ISOLATION_EM = 2.0  # 单字与同行其他词的距离 >= 此值 * 字号 才算"孤立"（范围 1.0–3.0）
VERTICAL_PITCH_EM = 3.0  # 竖排单字列相邻两字的纵向间距上限 / 字号（范围 2.0–5.0）
MAX_BANNER_SHARE = 0.5  # 重叠区间内跨栏横幅行占比上限：超过则不算栏间空白带
REASON_GUTTER = "多栏版面：纵向空白带"
REASON_VERTICAL = "多栏版面：竖排单字"


@dataclass(frozen=True)
class Gutter:
    x: float
    left: float  # 左侧文字的最右端
    right: float  # 右侧文字的最左端
    top: float
    bottom: float
    left_rows: int
    right_rows: int


@dataclass(frozen=True)
class ColumnVerdict:
    multicolumn: bool
    reason: str = ""
    gutter: Gutter | None = None


def _row_side(row: Sequence[Word], x: float) -> tuple[bool, bool, bool]:
    """(有词跨过 x, 有词整体在 x 左侧, 有词整体在 x 右侧)。"""
    return (
        any(w.x0 < x < w.x1 for w in row),
        any(w.x1 <= x for w in row),
        any(w.x0 >= x for w in row),
    )


def _mid(row: Sequence[Word]) -> float:
    return sum(w.mid_y for w in row) / len(row)


def _gutter_at(
    rows: Sequence[Sequence[Word]], x: float, min_width: float, lo: float, hi: float
) -> Gutter | None:
    """x 处的栏间空白带：跨过 x 的整宽行（横幅）不打断，但左侧行与右侧行必须在纵向上重叠，
    且重叠区间内两侧各 >= MIN_SIDE_ROWS 行（行内有两侧文字的行两侧都算一行）。"""
    banners: list[Sequence[Word]] = []
    left_rows: list[Sequence[Word]] = []
    right_rows: list[Sequence[Word]] = []
    for row in rows:
        cross, has_left, has_right = _row_side(row, x)
        if cross:
            banners.append(row)
            continue
        if has_left:
            left_rows.append(row)
        if has_right:
            right_rows.append(row)
    if len(left_rows) < MIN_SIDE_ROWS or len(right_rows) < MIN_SIDE_ROWS:
        return None
    left_mids = [_mid(r) for r in left_rows]
    right_mids = [_mid(r) for r in right_rows]
    y0, y1 = max(min(left_mids), min(right_mids)), min(max(left_mids), max(right_mids))
    if y0 > y1:
        return None
    in_left = [r for r, m in zip(left_rows, left_mids, strict=True) if y0 <= m <= y1]
    in_right = [r for r, m in zip(right_rows, right_mids, strict=True) if y0 <= m <= y1]
    if len(in_left) < MIN_SIDE_ROWS or len(in_right) < MIN_SIDE_ROWS:
        return None
    n_banner = sum(y0 <= _mid(r) <= y1 for r in banners)
    n_side = len({id(r) for r in (*in_left, *in_right)})
    if n_banner > MAX_BANNER_SHARE * (n_banner + n_side):
        return None
    left = max(w.x1 for r in in_left for w in r if w.x1 <= x)
    right = min(w.x0 for r in in_right for w in r if w.x0 >= x)
    if right - left < min_width or left < lo or right > hi:
        return None
    side = [*in_left, *in_right]
    return Gutter(
        x,
        left,
        right,
        min(w.top for r in side for w in r),
        max(w.bottom for r in side for w in r),
        len(in_left),
        len(in_right),
    )


def _vertical_stack(rows: Sequence[Sequence[Word]], body_size: float) -> bool:
    """竖排单字列：同一 x 上（x0 容差内成簇）连续 >= VERTICAL_MIN_CHARS 个孤立单字（汉字 / 字母 /
    数字不限），相邻两字的纵向间距 <= VERTICAL_PITCH_EM 个字号。"""
    em = body_size or 10.0
    singles: list[Word] = []
    for r in rows:
        for k, w in enumerate(r):
            if len(w.text) != 1:
                continue
            left_gap = w.x0 - r[k - 1].x1 if k > 0 else float("inf")
            right_gap = r[k + 1].x0 - w.x1 if k + 1 < len(r) else float("inf")
            if min(left_gap, right_gap) >= VERTICAL_ISOLATION_EM * em:
                singles.append(w)
    singles.sort(key=lambda w: w.x0)
    clusters: list[list[Word]] = []
    for w in singles:
        if clusters and w.x0 - clusters[-1][-1].x0 <= VERTICAL_X_TOL:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    for cluster in clusters:
        cluster.sort(key=lambda w: w.top)
        run = 1
        for a, b in itertools.pairwise(cluster):
            if 0 < b.top - a.top <= VERTICAL_PITCH_EM * em:
                run += 1
                if run >= VERTICAL_MIN_CHARS:
                    return True
            else:
                run = 1
    return False


def detect_columns(rows: Sequence[Sequence[Word]], width: float, body_size: float) -> ColumnVerdict:
    """rows：非表格区域的行（按纵向位置升序，行内按 x0 升序）。只看直立词。"""
    upright = [[w for w in r if w.upright] for r in rows]
    upright = [r for r in upright if r]
    if _vertical_stack(upright, body_size):
        return ColumnVerdict(True, REASON_VERTICAL)
    lo, hi = GUTTER_ZONE[0] * width, GUTTER_ZONE[1] * width
    min_width = MIN_GUTTER_EM * (body_size or 10.0)
    x = lo
    while x <= hi:
        g = _gutter_at(upright, x, min_width, lo, hi)
        if g is not None:
            return ColumnVerdict(True, REASON_GUTTER, g)
        x += X_STEP
    return ColumnVerdict(False)


def page_columns(layout: PageLayout, body_size: float) -> ColumnVerdict:
    rows = cluster_rows([w for w in layout.words if w.upright])
    in_table = {id(w) for reg in find_table_regions(rows) for r in reg for w in r}
    return detect_columns([r for r in rows if id(r[0]) not in in_table], layout.width, body_size)
