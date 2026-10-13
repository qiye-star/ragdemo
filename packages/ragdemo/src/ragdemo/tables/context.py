"""表格上下文：表名 / 单位 / 币种 / 列单位（表格处理改进计划 T6）。

单位绑定是"假通过"形态（单位错一个数量级，数字就差 10^4 或 10^8），所以全部规则偏向"不知道"：

- 声明 = `单位：…` 或 `币种：…`。"编制单位：某某公司"不是声明。
- 同一范围（表内 / 单个前文块）里出现两个不同的单位 → 冲突 → 单位未知（None），不取"第一处"。
- 范围里有"单位：股/万股/吨…"这类**非金额单位**声明 → 也算"已声明但不认识"：单位未知，
  并且**不再往更远的块里找**（否则会把更远处的"单位：元"错绑给持股表）。
- 前文块只看调用方给的最近几个、且长度 ≤ `MAX_DECL_BLOCK_CHARS` 的块；
  长段落里顺带提到的单位不算声明。
- 币种只在声明里认：`币种：美元`、`单位：美元/万美元/港元`。没写就是 None，**不默认人民币**；
  人民币与非人民币同时出现时报非人民币（让下游拒绝，而不是悄悄当人民币）。
- 单位优先级（消费者用）：格内行内单位 > 列单位 > 表单位（`unit_for_cell`）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from ragdemo.tables.grid import TableGrid

UNIT_SCALE: Final[Mapping[str, Decimal]] = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
}
UnitSource = Literal["in_table", "preceding_block", "none"]

MAX_DECL_BLOCK_CHARS: Final = 80
_CUR_CODE: Final = {"人民币": "CNY", "美元": "USD", "港元": "HKD"}
_ZH: Final = "一-鿿"

# 声明：单位：<尾部>。"编制单位"是公司名，不是量纲。
_DECL: Final = re.compile(r"(?<!编制)单位\s*[:：]\s*")
# 尾部：[币种]金额单位，其后不能紧跟别的汉字（"元宇宙…"），但允许紧跟"币种"。
_MONEY: Final = re.compile(
    rf"(?P<cur>人民币|美元|港元)?\s*(?P<unit>百万元|亿元|万元|千元|元)(?:(?![{_ZH}])|(?=币种))"
)
_FOREIGN: Final = re.compile(rf"(?:百万|千万|万|千|亿)?(?P<cur>美元|港元)(?![{_ZH}])")
_CUR_DECL: Final = re.compile(r"币种\s*[:：]?\s*(?P<cur>人民币|美元|港元)")
_COL_UNIT: Final = re.compile(r"[（(]\s*(?P<u>百万元|亿元|万元|千元|元|%|％)\s*[)）]")
_TAGS: Final = re.compile(r"<br\s*/?>", re.IGNORECASE)


@dataclass(frozen=True)
class _Scan:
    units: frozenset[str]  # 认得的金额单位
    unknown_decl: bool  # 有"单位：…"声明但不是认得的金额单位（股、吨、美元…）
    currencies: frozenset[str]  # "CNY" | "USD" | "HKD"

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
            units.add(mm.group("unit"))
            if mm.group("cur"):
                curs.add(_CUR_CODE[mm.group("cur")])
            continue
        fm = _FOREIGN.match(tail)
        if fm is not None:
            curs.add(_CUR_CODE[fm.group("cur")])
        elif tail.startswith("人民币"):
            curs.add("CNY")
        unknown = True  # 有声明，但量纲不是"元"系：不绑定，也不往远处找
    for m in _CUR_DECL.finditer(t):
        curs.add(_CUR_CODE[m.group("cur")])
    return _Scan(frozenset(units), unknown, frozenset(curs))


def _currency_of(curs: frozenset[str]) -> str | None:
    non_cny = sorted(c for c in curs if c != "CNY")
    if non_cny:
        return non_cny[0]
    return "CNY" if curs else None


def find_unit(text: str) -> str | None:
    """文本里唯一的"元"系金额单位；没有、冲突、或同时声明了不认识的单位 → None。"""
    return _scan(text).unit


def find_currency(text: str) -> str | None:
    """ "CNY" | "USD" | "HKD"；只认明文声明，不默认。有非人民币就报非人民币。"""
    return _currency_of(_scan(text).currencies)


@dataclass(frozen=True)
class TableContext:
    caption: str | None
    unit: str | None
    unit_source: UnitSource
    currency: str | None
    # (列号, 单位)；单位 "" = 该列表头里单位互相冲突（未知，不回落到表单位）
    column_units: tuple[tuple[int, str], ...] = ()

    def unit_for_col(self, col: int) -> str | None:
        for c, u in self.column_units:
            if c == col:
                return u or None
        return self.unit

    def unit_for_cell(self, col: int, inline_unit: str | None) -> str | None:
        """格内行内单位 > 列单位 > 表单位。"""
        return inline_unit if inline_unit else self.unit_for_col(col)


def _header_text(text: str) -> str:
    return " ".join(_TAGS.sub(" ", text).split())


def column_units(grid: TableGrid) -> tuple[tuple[int, str], ...]:
    per_col: dict[int, set[str]] = {}
    for cell in grid.cells:
        if cell.row >= grid.header_rows:
            continue
        if grid.n_cols >= 2 and cell.col == 0 and cell.col_span >= grid.n_cols:
            continue  # 通栏标题里的（万元）说的是整个标题，不是某一列
        m = _COL_UNIT.search(_header_text(cell.text))
        if m is None:
            continue
        u = "%" if m.group("u") == "％" else m.group("u")
        for col in range(cell.col, cell.col + cell.col_span):
            per_col.setdefault(col, set()).add(u)
    return tuple((c, next(iter(us)) if len(us) == 1 else "") for c, us in sorted(per_col.items()))


def bind_context(
    grid: TableGrid, *, caption: str | None, preceding_texts: Sequence[str]
) -> TableContext:
    """表内 → 前文块（近者优先）→ 不知道。冲突/不认识的声明 → 单位 None，且不再往远处找。"""
    scopes: list[tuple[UnitSource, _Scan]] = [
        ("in_table", _scan("\n".join(c.text for c in grid.cells)))
    ]
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
    return TableContext(caption, unit, source, currency, column_units(grid))


def textin_caption(detail: Sequence[Mapping[str, Any]], item: Mapping[str, Any]) -> str | None:
    cap = item.get("caption_id")
    if not isinstance(cap, Mapping) or "page_id" not in cap or "paragraph_id" not in cap:
        return None
    for d in detail:
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
    """表格之前最近的 `limit` 个正文块文本（近→远）；跳过页眉页脚（content == 1）、表格、空文本。"""
    out: list[str] = []
    for j in range(index - 1, -1, -1):
        if len(out) >= limit:
            break
        d = detail[j]
        if int(d.get("content", 0) or 0) == 1 or d.get("type") == "table":
            continue
        text = str(d.get("text") or "").strip()
        if text:
            out.append(text)
    return out
