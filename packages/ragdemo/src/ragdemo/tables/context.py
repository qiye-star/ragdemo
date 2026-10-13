"""表格上下文：表名 / 单位 / 币种 / 列单位 / 行单位（表格处理改进计划 T6）。

单位绑定是"假通过"形态（单位错一个数量级，数字就差 10^4 或 10^8），所以全部规则偏向"不知道"：

- 声明 = `单位：…` 或 `币种：…`。"编制单位：某某公司"不是声明。
- 同一范围（表内 / 单个前文块）里出现两个不同的单位 → 冲突 → 单位未知（None），不取"第一处"。
- 复合单位（`万元、%`、`万元/%`、`元/股`、`万元/吨`）= 未知；只有列单位覆盖到的列才有单位。
- 范围里有"单位：股/万股/吨…"这类**非金额单位**声明 → 也算"已声明但不认识"：单位未知，
  并且**不再往更远的块里找**（否则会把更远处的"单位：元"错绑给持股表）。
- 币种声明（`币种：X`、`单位：美元`、`单位：元 US$`）只认明文：人民币/RMB/CNY → CNY，
  美元/美金/USD/US$ → USD，港元/港币/HKD → HKD，欧元 EUR，日元 JPY，英镑 GBP；
  其他任何 `币种：X` → 币种 "OTHER" 且单位未知。没写币种就是 None（**不默认人民币**）；
  人民币与非人民币同时出现时报非人民币。下游：币种不在 (None, "CNY") → 拒绝，不换算。
- 前文块由调用方给（`textin_preceding_texts`：最近 2 个正文块，**不越过表格**，只有紧挨着的
  上一页末尾的表才算同一张表的续表）；长度 > `MAX_DECL_BLOCK_CHARS` 的块不算声明。
- 表内声明只看表头区与通栏格；表体行标签里的声明（"投入环保资金(单位:万元)"）只属于那一行。
- 单位优先级（消费者用 `unit_for_cell`）：格内行内单位 > 列单位 > 表单位；行标签声明的单位
  （`（元/股）`、`（%）`、`（人）`、比例/占比/收益率一类词、美元/港元…）与列/表单位冲突 → None。
- `unit_for_col` / `unit_for_cell` 可能返回 `UNIT_SCALE` 之外的单位（万美元、股、%、人…）：
  调用方必须当作**不可换算**（`UNIT_SCALE[u]` 会 KeyError，要用 `.get`）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from ragdemo.tables.grid import TableGrid
from ragdemo.tables.headers import row_label
from ragdemo.tables.numbers import cell_value

UNIT_SCALE: Final[Mapping[str, Decimal]] = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
}
UnitSource = Literal["in_table", "preceding_block", "none"]

MAX_DECL_BLOCK_CHARS: Final = 80
_ZH: Final = "一-鿿"

_CUR_ALIASES: Final = {
    "人民币": "CNY",
    "RMB": "CNY",
    "CNY": "CNY",
    "美元": "USD",
    "美金": "USD",
    "USD": "USD",
    "US$": "USD",
    "港元": "HKD",
    "港币": "HKD",
    "HKD": "HKD",
    "HK$": "HKD",
    "欧元": "EUR",
    "EUR": "EUR",
    "日元": "JPY",
    "JPY": "JPY",
    "英镑": "GBP",
    "GBP": "GBP",
}
_CUR_ALT: Final = "|".join(re.escape(k) for k in sorted(_CUR_ALIASES, key=len, reverse=True))
_NON_CNY_ALT: Final = "|".join(
    re.escape(k) for k in sorted(_CUR_ALIASES, key=len, reverse=True) if _CUR_ALIASES[k] != "CNY"
)


def _cur(word: str) -> str:
    return _CUR_ALIASES[word.upper() if word.upper() in _CUR_ALIASES else word]


# 声明：单位：<尾部>。"编制单位"是公司名，不是量纲。
_DECL: Final = re.compile(r"(?<!编制)单位\s*[:：]\s*")
# 尾部：[币种]金额单位，其后不能紧跟别的汉字（"元宇宙…"），但允许紧跟"币种"。
_MONEY: Final = re.compile(
    rf"(?:(?P<cur>{_CUR_ALT})\s*)?(?P<unit>百万元|亿元|万元|千元|元)(?:(?![{_ZH}])|(?=币种))",
    re.IGNORECASE,
)
_FOREIGN: Final = re.compile(
    rf"(?:百万|千万|万|千|亿)?(?P<cur>{_NON_CNY_ALT})(?:元)?(?![{_ZH}])", re.IGNORECASE
)
_AFTER_UNIT_CUR: Final = re.compile(rf"\s*(?P<cur>{_NON_CNY_ALT})", re.IGNORECASE)
_MIXED: Final = re.compile(r"\s*(?:[/／]|[、,，和及与]\s*(?=[%％/／元股人户个吨倍万台件百千亿]))")
_CUR_COLON: Final = re.compile(r"币种\s*[:：]\s*(?P<tail>[^\s,，;；。)）]{0,8})")
_CUR_BARE: Final = re.compile(rf"币种\s*(?P<cur>{_CUR_ALT})", re.IGNORECASE)
_CUR_HEAD: Final = re.compile(rf"(?P<cur>{_CUR_ALT})", re.IGNORECASE)
_TAGS: Final = re.compile(r"<br\s*/?>", re.IGNORECASE)

_UNIT_ALT: Final = (
    "百万美元|亿美元|万美元|千美元|百万元|亿元|万元|千元|元/股|元/[一-鿿]{1,2}"
    "|美元|港元|欧元|日元|英镑|元|%|％|万股|亿股|股|万人|人|户|个|次|倍|万吨|吨|家|万台|台"
    "|万件|件|年|天"
)
_BRACKET_UNIT: Final = re.compile(rf"[（(]\s*(?:单位\s*[:：]\s*)?(?P<u>{_UNIT_ALT})\s*[)）]")
_FOREIGN_WORD: Final = re.compile("美元|港元|港币|美金|欧元|日元|英镑|外币")
_RATIO_WORD: Final = re.compile(
    "比例|比率|占比|比重|收益率|毛利率|增长率|增减率|变动幅度|增减幅度|利润率|周转率"
)
_FOREIGN_MARK: Final = "外币"


@dataclass(frozen=True)
class _Scan:
    units: frozenset[str]  # 认得的金额单位
    unknown_decl: bool  # 有声明但不是认得的金额单位/币种（股、吨、美元、卢布…）
    currencies: frozenset[str]  # "CNY" | "USD" | "HKD" | "EUR" | "JPY" | "GBP" | "OTHER"

    @property
    def has_unit_decl(self) -> bool:
        return bool(self.units) or self.unknown_decl

    @property
    def unit(self) -> str | None:
        return next(iter(self.units)) if len(self.units) == 1 and not self.unknown_decl else None


def _scan(text: str) -> _Scan:
    t = " ".join(_TAGS.sub(" ", text).split())
    units: set[str] = set()
    curs: set[str] = set()
    unknown = False
    for m in _DECL.finditer(t):
        tail = t[m.end() :]
        mm = _MONEY.match(tail)
        if mm is not None:
            if mm.group("cur"):
                curs.add(_cur(mm.group("cur")))
            rest = tail[mm.end() :]
            ac = _AFTER_UNIT_CUR.match(rest)
            if ac is not None:
                curs.add(_cur(ac.group("cur")))
            if _MIXED.match(rest) is not None:
                unknown = True  # 复合单位：不绑定
            else:
                units.add(mm.group("unit"))
            continue
        fm = _FOREIGN.match(tail)
        if fm is not None:
            curs.add(_cur(fm.group("cur")))
        elif tail.startswith("人民币"):
            curs.add("CNY")
        unknown = True  # 有声明，但量纲不是"元"系：不绑定，也不往远处找
    for m in _CUR_COLON.finditer(t):
        tail = m.group("tail")
        hm = _CUR_HEAD.match(tail)
        if hm is not None:
            curs.add(_cur(hm.group("cur")))
        elif tail:
            curs.add("OTHER")
            unknown = True  # 币种声明了但不认识：单位一并未知
    for m in _CUR_BARE.finditer(t):
        curs.add(_cur(m.group("cur")))
    return _Scan(frozenset(units), unknown, frozenset(curs))


def _currency_of(curs: frozenset[str]) -> str | None:
    non_cny = sorted(c for c in curs if c != "CNY")
    if non_cny:
        return non_cny[0]
    return "CNY" if curs else None


def find_unit(text: str) -> str | None:
    """文本里唯一的"元"系金额单位；没有、冲突、复合、或同时声明了不认识的单位 → None。"""
    return _scan(text).unit


def find_currency(text: str) -> str | None:
    """ "CNY" | "USD" | "HKD" | "EUR" | "JPY" | "GBP" | "OTHER"；只认明文声明，不默认。"""
    return _currency_of(_scan(text).currencies)


@dataclass(frozen=True)
class TableContext:
    caption: str | None
    unit: str | None
    unit_source: UnitSource
    currency: str | None
    # (列号, 单位)；单位 "" = 该列表头里单位互相冲突（未知，不回落到表单位）。
    # 单位可能是 "%"、"股"、"万美元" 等不可换算的标记。
    column_units: tuple[tuple[int, str], ...] = ()
    # (行号, 标记)：行标签里声明的单位（括号单位、比例类词 → "%"、外币词 → "外币"）；"" = 冲突。
    row_units: tuple[tuple[int, str], ...] = ()

    def unit_for_col(self, col: int) -> str | None:
        for c, u in self.column_units:
            if c == col:
                return u or None
        return self.unit

    def unit_for_cell(self, row: int, col: int, inline_unit: str | None) -> str | None:
        """格内行内单位 > 列单位 > 表单位；行标签声明的单位与之冲突 → None。

        返回值可能不在 `UNIT_SCALE` 里（%、股、人…）：不可换算。
        """
        if inline_unit:
            return inline_unit
        col_decl = dict(self.column_units).get(col)
        if col_decl == "":
            return None
        eff = col_decl if col_decl is not None else self.unit
        row_u = dict(self.row_units).get(row)
        if row_u is None:
            return eff
        if row_u in ("", _FOREIGN_MARK):
            return None
        if eff is None:
            return row_u
        return row_u if row_u == eff else None


def _norm(text: str) -> str:
    return " ".join(_TAGS.sub(" ", text).split())


def _markers(text: str, *, trailing_percent: bool) -> set[str]:
    t = _norm(text)
    out: set[str] = set()
    for m in _BRACKET_UNIT.finditer(t):
        u = m.group("u")
        if u == "％":
            u = "%"
        elif _FOREIGN_WORD.search(u):
            u = _FOREIGN_MARK
        out.add(u)
    if _FOREIGN_WORD.search(t):
        out.add(_FOREIGN_MARK)
    if _RATIO_WORD.search(t):
        out.add("%")
    if trailing_percent and re.search(r"[%％]\s*$", t):
        out.add("%")
    return out


def _is_full_width(grid: TableGrid, col: int, col_span: int) -> bool:
    return grid.n_cols >= 2 and col == 0 and col_span >= grid.n_cols


def column_units(grid: TableGrid) -> tuple[tuple[int, str], ...]:
    per_col: dict[int, set[str]] = {}
    for cell in grid.cells:
        if cell.row >= grid.header_rows or _is_full_width(grid, cell.col, cell.col_span):
            continue  # 通栏标题里的（万元）说的是整个标题，不是某一列
        for u in _markers(cell.text, trailing_percent=True):
            for col in range(cell.col, cell.col + cell.col_span):
                per_col.setdefault(col, set()).add(u)
    return tuple((c, next(iter(us)) if len(us) == 1 else "") for c, us in sorted(per_col.items()))


def row_units(grid: TableGrid) -> tuple[tuple[int, str], ...]:
    out: list[tuple[int, str]] = []
    for r in range(grid.header_rows, grid.n_rows):
        label = row_label(grid, r)
        if not label and grid.header_cols == 0:
            c0 = grid.cell_at(r, 0)
            if c0 is not None and cell_value(c0.text) is None:
                label = c0.text
        if not label:
            continue
        us = _markers(label, trailing_percent=False)
        if us:
            out.append((r, next(iter(us)) if len(us) == 1 else ""))
    return tuple(out)


def _table_scan_text(grid: TableGrid) -> str:
    """表内声明只看表头区与通栏格；表体行标签里的声明只属于那一行。"""
    label_cols = max(grid.header_cols, 1)
    parts = []
    for c in grid.cells:
        is_label = (
            c.row >= grid.header_rows
            and c.col < label_cols
            and not _is_full_width(grid, c.col, c.col_span)
        )
        if not is_label:
            parts.append(c.text)
    return "\n".join(parts)


def bind_context(
    grid: TableGrid, *, caption: str | None, preceding_texts: Sequence[str]
) -> TableContext:
    """表内 → 前文块（近者优先）→ 不知道。冲突/不认识的声明 → 单位 None，且不再往远处找。"""
    scopes: list[tuple[UnitSource, _Scan]] = [("in_table", _scan(_table_scan_text(grid)))]
    scopes += [
        ("preceding_block", _scan(t)) for t in preceding_texts if len(t) <= MAX_DECL_BLOCK_CHARS
    ]
    unit: str | None = None
    source: UnitSource = "none"
    last = len(scopes)
    for i, (src, scan) in enumerate(scopes):
        if scan.has_unit_decl:
            unit = scan.unit
            source = src if unit is not None else "none"
            last = i + 1
            break
    # 币种：在单位被决定（或被拦住）的范围之内取第一个有币种声明的范围
    currency: str | None = None
    for _, scan in scopes[:last]:
        if scan.currencies:
            currency = _currency_of(scan.currencies)
            break
    return TableContext(caption, unit, source, currency, column_units(grid), row_units(grid))


def textin_caption(detail: Sequence[Mapping[str, Any]], item: Mapping[str, Any]) -> str | None:
    """caption_id 指向的段落文本；只接受**排在表格之前**的段落（指向表后的标题属于下一节）。"""
    cap = item.get("caption_id")
    if not isinstance(cap, Mapping) or "page_id" not in cap or "paragraph_id" not in cap:
        return None
    table_idx = next((k for k, d in enumerate(detail) if d is item), None)
    if table_idx is None:
        return None
    for k in range(table_idx):
        d = detail[k]
        if (
            d.get("type") != "table"
            and d.get("page_id") == cap["page_id"]
            and d.get("paragraph_id") == cap["paragraph_id"]
        ):
            text = str(d.get("text") or "").strip()
            return text or None
    return None


def textin_preceding_texts(
    detail: Sequence[Mapping[str, Any]], index: int, *, limit: int = 2
) -> list[str]:
    """表格之前最近的 `limit` 个正文块文本（近→远）；跳过页眉页脚（content == 1）和空文本。

    **不越过表格**：中间隔着别的表，单位就不属于这张表。唯一例外是跨页续表：紧挨着
    （只隔页眉页脚）的上一张表在上一页，则视为同一张表的前半，继续往它前面找。
    """
    out: list[str] = []
    page = detail[index].get("page_id")
    for j in range(index - 1, -1, -1):
        if len(out) >= limit:
            break
        d = detail[j]
        if int(d.get("content", 0) or 0) == 1:
            continue
        if d.get("type") == "table":
            prev_page = d.get("page_id")
            if (
                not out
                and isinstance(page, int)
                and isinstance(prev_page, int)
                and prev_page == page - 1
            ):
                page = prev_page
                continue
            break
        text = str(d.get("text") or "").strip()
        if text:
            out.append(text)
    return out
