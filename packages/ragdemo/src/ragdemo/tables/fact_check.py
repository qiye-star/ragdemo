"""结构化事实（Wind）↔ 已解析表格单元格的逐格校验（表格处理改进计划 T18）。

不是事实来源，也不改任何事实：结果只写进 `core.fact_table_ref`（迁移 031），`fin_fact` 一行不动。

匹配规则全部写死、保守，含糊就记 `ambiguous`，绝不强行判定：
- 行：科目标签（`FACT_LABELS`，经 `normalise_name` / 报表前缀剥离后整格相等）；
- 列：列表头经 `parse_period_path` 解析后必须**恰好等于** `PeriodRef(报告期, 期望口径)`；
  流量科目期望 `ytd`（年初至报告期末累计），时点科目期望 `point`。同一行上期别相同的列有 ≥ 2 个、
  或唯一一列带"调整前/调整后/预计…"限定词 → `ambiguous`；
- 现金流量表里 `single_quarter` 口径不采信（"本期金额"在累计口径的报表里是累计）；
- 母公司表（caption 含"母公司"）与表头含"母公司"的列永远不作为合并口径事实的佐证；
- 非日历财年：Q1 判定需要 `TableView.calendar_fy is True`，缺标记 → `ambiguous`；
- 佐证格必须 `check == "match"`、有页码与 bbox、跨度为 1、整格是唯一一个金额；
- 单位：`TableContext.unit_for_cell`（行内 > 列 > 表单位，行标签冲突 → 未知）；不在
  `UNIT_SCALE` 的单位 / 币种不是 (None, "CNY") 一律拒绝，不换算；
- 数值比较只用 Python `Decimal`；模型不产生任何数字。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal, cast

import psycopg

from ragdemo.ingest.stage2_crosscheck import (
    FACT_LABELS,
    FACT_TOLERANCE,
    crosscheck_facts,
    normalise_name,
)
from ragdemo.tables.context import UNIT_SCALE, TableContext
from ragdemo.tables.grid import TableGrid, grid_from_json
from ragdemo.tables.headers import col_header_path, row_label
from ragdemo.tables.numbers import cell_value
from ragdemo.tables.periods import PeriodBasis, PeriodRef, doc_period_of_title, parse_period_path
from ragdemo.tables.statements import _key as _statement_key

FACT_CHECK_VERSION: Final = "factcell:v1"

METRIC_KIND: Final[Mapping[str, Literal["flow", "point"]]] = {
    "revenue_total": "flow",
    "net_profit_attributable": "flow",
    "rd_expense": "flow",
    "operating_cash_flow": "flow",
    "capex": "flow",
    "inventory": "point",
    "contract_liability": "point",
    "prepayment": "point",
}
# 假设（需数据检验，见验收记录）：Wind 的 Q3 流量科目是年初至报告期末累计口径。
EXPECTED_BASIS: Final[Mapping[str, PeriodBasis]] = {"flow": "ytd", "point": "point"}

Verdict = Literal[
    "match", "mismatch", "ambiguous", "no_row", "no_period_column", "no_unit", "not_cny"
]
WRITTEN_VERDICTS: Final = frozenset({"match", "mismatch"})

_QUALIFIERS: Final = (
    "调整前", "调整后", "重述", "重列", "追溯", "预计", "预测", "预期", "备考", "模拟", "变动前",
    "变动后", "更正",
)  # fmt: skip
_PARENT: Final = "母公司"
_SEP: Final = " / "
# 阶段越靠后越"接近"事实：用于多张表都没命中时选最有信息量的原因。
_STAGE_RANK: Final = {"not_cny": 3, "no_unit": 3, "no_period_column": 2, "no_row": 1}


@dataclass(frozen=True)
class FactCheckConfig:
    allow_preceding_unit: bool = True


@dataclass(frozen=True)
class FactRow:
    fact_id: int
    entity_id: str
    metric_id: str
    period: str
    value: Decimal
    unit: str
    known_at: datetime
    valid_from: date


@dataclass(frozen=True)
class TableView:
    table_id: int
    block_id: int
    doc_period: str | None
    grid: TableGrid
    unit: str | None
    unit_source: str
    column_units: tuple[tuple[int, str], ...]
    row_units: tuple[tuple[int, str], ...]
    currency: str | None
    statement_kind: str
    known_at: datetime
    caption: str | None = None
    # 财年末是否为 12-31：A 股强制日历财年（加载器置 True）；未知 = None，Q1 判定不采信。
    calendar_fy: bool | None = None


@dataclass(frozen=True)
class FactCellMatch:
    fact_id: int
    verdict: Verdict
    table_id: int | None
    block_id: int | None
    row: int | None
    col: int | None
    page: int | None
    report_value: Decimal | None
    tolerance: Decimal | None
    unit_scale: Decimal | None
    period_label: str
    known_at: datetime | None
    reason: str = ""
    entity_id: str = ""
    metric_id: str = ""
    period: str = ""
    fact_value: Decimal | None = None
    cell_text: str = ""


# ---- 单元格数值 ----------------------------------------------------------------------------


def _scale_of(unit: str | None) -> Decimal | None:
    return UNIT_SCALE.get(unit) if unit else None


def cell_value_in_unit(text: str, unit: str | None) -> tuple[Decimal, Decimal] | None:
    """`(换算后的元值, 倍数)`；整格必须恰好一个非百分数金额。行内单位覆盖 `unit`。"""
    num = cell_value(text)
    if num is None or num.kind != "amount":
        return None
    scale = _scale_of(num.inline_unit or unit)
    if scale is None:
        return None
    return num.value * scale, scale


def tolerance_for(text: str, unit: str) -> Decimal:
    """`max(FACT_TOLERANCE, 0.5 × 10^(-小数位) × 倍数)`：报表四舍五入到小数位的半个单位。"""
    num = cell_value(text)
    if num is None:
        return FACT_TOLERANCE
    scale = _scale_of(num.inline_unit or unit)
    if scale is None:
        return FACT_TOLERANCE
    exponent = cast(int, num.value.as_tuple().exponent)
    decimals = max(0, -exponent)
    return max(FACT_TOLERANCE, Decimal("0.5") * Decimal(10) ** (-decimals) * scale)


# ---- 单表评估 ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Hit:
    table: TableView
    row: int
    col: int
    page: int
    report_value: Decimal
    tolerance: Decimal
    scale: Decimal
    label: str
    check: str
    cell_text: str
    own_doc: bool


@dataclass
class _TableOutcome:
    hits: list[_Hit] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    stage: str = "no_row"  # no_row < no_period_column < no_unit / not_cny
    reason: str = "no_label_row"

    def note(self, stage: str, reason: str) -> None:
        if _STAGE_RANK[stage] >= _STAGE_RANK[self.stage]:
            self.stage, self.reason = stage, reason


# T18 专用标签集：Wind 的 revenue_total 是"营业收入"（adapters/wind.py），不是"营业总收入"
# （金融类公司两者不同）。被排除的标签只作"兄弟行"：同表两行都在且数值不同 → ambiguous。
_EXCLUDED_LABELS: Final[Mapping[str, tuple[str, ...]]] = {"revenue_total": ("营业总收入",)}
# 精度下限：容差不得超过"报表至少保留两位小数"对应的半个单位（见迁移 031 的 CHECK）。
MIN_VALUE_TO_TOLERANCE: Final = Decimal(10)  # |表值| 至少是容差的 10 倍，否则分辨力不足
_PAREN_QUALIFIER: Final = re.compile(r"[（(][^()（）]*[)）]")


def max_tolerance(scale: Decimal) -> Decimal:
    """与迁移 031 `fact_table_ref_tolerance_bounded` 同一公式：max(6000, 0.005 × 倍数)。"""
    return max(FACT_TOLERANCE, Decimal("0.005") * scale)


def _keys_of(labels: Sequence[str]) -> frozenset[str]:
    keys: set[str] = set()
    for label in labels:
        keys.add(normalise_name(label))
        keys.add(_statement_key(label))
    return frozenset(keys)


def _label_keys(metric_id: str) -> frozenset[str]:
    excluded = _keys_of(_EXCLUDED_LABELS.get(metric_id, ()))
    return _keys_of(FACT_LABELS.get(metric_id, ())) - excluded


def _sibling_keys(metric_id: str) -> frozenset[str]:
    return _keys_of(_EXCLUDED_LABELS.get(metric_id, ()))


def _row_subject(grid: TableGrid, r: int) -> tuple[str, bool]:
    """行的科目名与"是否带限定词"。只看叶子标签（最后一段）；叶子本身只是限定词
    （"调整前"）时科目取上一层，并标记为带限定词。父标签匹配不会让子行变成科目行。"""
    parts = [p for p in row_label(grid, r).split(_SEP) if p]
    if not parts:
        return "", False
    leaf = parts[-1]
    if not any(q in leaf for q in _QUALIFIERS):
        return leaf, False
    stripped = _PAREN_QUALIFIER.sub(
        lambda m: "" if any(q in m.group(0) for q in _QUALIFIERS) else m.group(0), leaf
    )
    for q in _QUALIFIERS:
        stripped = stripped.replace(q, "")
    stripped = stripped.strip(" 、:：-—")
    if not stripped and len(parts) >= 2:
        stripped = parts[-2]
    return stripped, True


def _classify_rows(
    grid: TableGrid, keys: frozenset[str], sibling: frozenset[str]
) -> tuple[list[tuple[int, bool]], list[int]]:
    primary: list[tuple[int, bool]] = []
    siblings: list[int] = []
    for r in range(grid.header_rows, grid.n_rows):
        subject, qualified = _row_subject(grid, r)
        if not subject:
            continue
        hit_keys = {normalise_name(subject), _statement_key(subject)}
        if hit_keys & keys:
            primary.append((r, qualified))
        elif hit_keys & sibling and not qualified:
            siblings.append(r)
    return primary, siblings


def _is_qualified(path: Sequence[str]) -> bool:
    text = "".join(path)
    return any(q in text for q in _QUALIFIERS)


def _period_columns(
    tv: TableView, row: int, target: PeriodRef
) -> list[tuple[int, tuple[str, ...]]]:
    grid = tv.grid
    cols: list[tuple[int, tuple[str, ...]]] = []
    for col in range(grid.header_cols, grid.n_cols):
        path = col_header_path(grid, col, row=row)
        if not path or _PARENT in "".join(path):
            continue
        ref = parse_period_path(path, doc_period=tv.doc_period)
        if ref is None:
            continue
        if tv.statement_kind == "cash_flow" and ref.basis == "single_quarter":
            continue  # 现金流量表的"本期金额/本报告期"口径由报表类型裁决，这里不采信
        if ref == target:
            cols.append((col, path))
    return cols


def _row_hit(
    tv: TableView,
    ctx: TableContext,
    r: int,
    cols: list[tuple[int, tuple[str, ...]]],
    out: _TableOutcome,
    *,
    own_doc: bool,
) -> _Hit | None:
    """一行 + 唯一的期别列 → 命中；任何不确定记入 `out`（ambiguous / 阶段原因）并返回 None。"""
    if len(cols) > 1:
        out.ambiguous.append("duplicate_period_column")
        return None
    col, path = cols[0]
    if _is_qualified(path):
        out.ambiguous.append("qualified_period_column")
        return None
    cell = tv.grid.cell_at(r, col)
    if cell is None or cell.row_span != 1 or cell.col_span != 1:
        out.note("no_period_column", "cell_missing_or_merged")
        return None
    if cell.check != "match" or cell.page is None or cell.bbox is None:
        out.note("no_period_column", f"cell_not_verified:{cell.check}")
        return None
    num = cell_value(cell.text)
    if num is None or num.kind != "amount":
        out.note("no_period_column", "cell_not_a_single_amount")
        return None
    unit = ctx.unit_for_cell(r, col, num.inline_unit)
    if unit is None:
        out.note("no_unit", "no_unit")
        return None
    scale = _scale_of(unit)
    if scale is None:
        out.note("no_unit", f"unit_not_convertible:{unit}")
        return None
    tol = tolerance_for(cell.text, unit)
    value = num.value * scale
    if tol > max_tolerance(scale):
        out.ambiguous.append("insufficient_precision")  # 报表数字保留位数太少，容差撑得过大
        return None
    if abs(value) < MIN_VALUE_TO_TOLERANCE * tol:
        out.ambiguous.append("value_below_resolution")  # 数值与容差同量级，"一致"没有信息量
        return None
    return _Hit(
        tv, cell.row, cell.col, cell.page, value, tol, scale, _SEP.join(path), cell.check,
        cell.text, own_doc=own_doc,
    )  # fmt: skip


def _eval_table(
    fact: FactRow,
    tv: TableView,
    *,
    keys: frozenset[str],
    sibling: frozenset[str],
    target: PeriodRef,
    report_period: str,
    config: FactCheckConfig,
) -> _TableOutcome:
    out = _TableOutcome()
    grid = tv.grid
    primary, siblings = _classify_rows(grid, keys, sibling)
    if not primary:
        return out
    out.stage, out.reason = "no_period_column", "no_period_column"
    if report_period.endswith("Q1") and tv.calendar_fy is not True:
        out.ambiguous.append("fiscal_year_flag_missing")
        return out
    if tv.currency not in (None, "CNY"):
        out.note("not_cny", f"table_currency:{tv.currency}")
        return out
    table_unit = (
        None
        if (tv.unit_source == "preceding_block" and not config.allow_preceding_unit)
        else tv.unit
    )
    ctx = TableContext(
        tv.caption, table_unit, "in_table", tv.currency, tv.column_units, tv.row_units
    )
    own_doc = tv.doc_period == report_period
    cand = [(r, q, cols) for r, q in primary if (cols := _period_columns(tv, r, target))]
    if not cand:
        return out
    if any(q for _, q, _ in cand):
        out.ambiguous.append("qualified_row_label")  # 调整前/调整后等行：不知道 Wind 用的是哪个
        return out
    if len(cand) > 1:
        out.ambiguous.append("multiple_label_rows")  # 同表多行同科目：不挑一行
        return out
    r, _, cols = cand[0]
    hit = _row_hit(tv, ctx, r, cols, out, own_doc=own_doc)
    if hit is None:
        return out
    for sr in siblings:  # 例如营业总收入：与科目行不一致 → 口径含糊
        scols = _period_columns(tv, sr, target)
        if len(scols) != 1:
            continue
        sib = _row_hit(tv, ctx, sr, scols, _TableOutcome(), own_doc=own_doc)
        if sib is not None and abs(sib.report_value - hit.report_value) > max(
            sib.tolerance, hit.tolerance
        ):
            out.ambiguous.append("sibling_label_differs")
            return out
    out.hits.append(hit)
    return out


# ---- 一条事实 ------------------------------------------------------------------------------


def _result(fact: FactRow, verdict: Verdict, reason: str, hit: _Hit | None = None) -> FactCellMatch:
    base: dict[str, Any] = {
        "reason": reason,
        "entity_id": fact.entity_id,
        "metric_id": fact.metric_id,
        "period": fact.period,
        "fact_value": fact.value,
    }
    if hit is None:
        return FactCellMatch(
            fact.fact_id, verdict, None, None, None, None, None, None, None, None, "", None, **base
        )
    return FactCellMatch(
        fact.fact_id,
        verdict,
        hit.table.table_id,
        hit.table.block_id,
        hit.row,
        hit.col,
        hit.page,
        hit.report_value,
        hit.tolerance,
        hit.scale,
        hit.label,
        max(fact.known_at, hit.table.known_at),
        cell_text=hit.cell_text,
        **base,
    )


def match_fact(
    fact: FactRow,
    tables: Sequence[TableView],
    *,
    config: FactCheckConfig = FactCheckConfig(),  # noqa: B008
) -> FactCellMatch:
    if fact.unit != "CNY":
        return _result(fact, "not_cny", f"fact_unit:{fact.unit}")
    kind = METRIC_KIND.get(fact.metric_id)
    keys = _label_keys(fact.metric_id)
    sibling = _sibling_keys(fact.metric_id)
    if kind is None or not keys:
        return _result(fact, "no_row", "metric_not_supported")
    report_period = fact.period.replace("Q2", "H1")
    target = PeriodRef(report_period, EXPECTED_BASIS[kind])

    outcomes: list[_TableOutcome] = []
    parent_has_row = False
    for tv in tables:
        if _PARENT in (tv.caption or ""):
            parent_has_row = parent_has_row or bool(_classify_rows(tv.grid, keys, sibling)[0])
            continue
        outcomes.append(
            _eval_table(
                fact,
                tv,
                keys=keys,
                sibling=sibling,
                target=target,
                report_period=report_period,
                config=config,
            )
        )

    hits = [h for o in outcomes for h in o.hits]
    ambiguous = sorted({a for o in outcomes for a in o.ambiguous})
    matched = [h for h in hits if abs(h.report_value - fact.value) <= h.tolerance]
    own_hits = [h for h in hits if h.own_doc]
    own_matched = [h for h in matched if h.own_doc]

    def _best_match(pool: list[_Hit]) -> _Hit:
        return min(pool, key=lambda h: (h.table.table_id, h.row, h.col))

    if own_matched:
        if len(own_matched) < len(own_hits):
            return _result(fact, "ambiguous", "own_report_hits_disagree", _best_match(own_matched))
        return _result(fact, "match", "", _best_match(own_matched))
    if own_hits:
        # 本期报告里对不上：若别处（后续报告的比较列）对得上，正是"Wind 值是追溯调整后的"的形态。
        if matched:
            return _result(
                fact, "ambiguous", "own_report_mismatch_comparative_match", _best_match(matched)
            )
        if ambiguous:
            return _result(fact, "ambiguous", ambiguous[0])
        best = min(own_hits, key=lambda h: (abs(h.report_value - fact.value), h.table.table_id))
        return _result(fact, "mismatch", "own_document", best)
    if matched:
        if len(matched) < len(hits):
            return _result(fact, "ambiguous", "comparative_hits_disagree", _best_match(matched))
        return _result(fact, "match", "comparative_column", _best_match(matched))
    if ambiguous:
        return _result(fact, "ambiguous", ambiguous[0])
    if hits:
        # 只在别的期别的报告里作为比较列出现：该列可能是追溯调整后的数，不当作"不一致"落库。
        best = min(hits, key=lambda h: (abs(h.report_value - fact.value), h.table.table_id))
        return _result(fact, "ambiguous", "comparative_mismatch_possible_restatement", best)
    if outcomes:
        worst = max(outcomes, key=lambda o: _STAGE_RANK[o.stage])
        if worst.stage != "no_row" or not parent_has_row:
            return _result(fact, cast(Verdict, worst.stage), worst.reason)
    if parent_has_row:
        return _result(fact, "no_row", "parent_company_only")
    return _result(fact, "no_row", "no_candidate_table")


# ---- 报告 ----------------------------------------------------------------------------------


def period_kind(period: str) -> str:
    return period[4:].replace("Q2", "H1")


@dataclass
class FactCheckReport:
    results: list[FactCellMatch]
    written: int = 0
    superseded: int = 0
    tables_considered: int = 0
    tables_with_hits: int = 0

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.verdict for r in self.results))

    def reasons(self) -> dict[tuple[str, str], int]:
        return dict(Counter((r.verdict, r.reason) for r in self.results))

    def by_period_kind(self) -> dict[str, tuple[int, int]]:
        """`Q1/H1/Q3/FY` → (match 数, 已定位数 = match + mismatch)。"""
        out: dict[str, tuple[int, int]] = {}
        for r in self.results:
            if r.verdict not in WRITTEN_VERDICTS:
                continue
            m, n = out.get(period_kind(r.period), (0, 0))
            out[period_kind(r.period)] = (m + (r.verdict == "match"), n + 1)
        return out


# ---- 读库 / 写库 ---------------------------------------------------------------------------

_FACTS_SQL = """
SELECT fact_id, entity_id, metric_id, period, value, unit, known_at, valid_from
  FROM core.fin_fact WHERE source = 'wind' AND superseded_at IS NULL ORDER BY fact_id
"""

_TABLES_SQL = """
SELECT t.table_id, t.block_id, t.entity_id, d.title, t.grid, t.unit, t.unit_source,
       t.column_units, t.row_units, t.currency, t.statement_kind, t.known_at, t.caption
  FROM core.doc_table t
  JOIN core.document d ON d.doc_id = t.doc_id
 WHERE t.superseded_at IS NULL AND t.owner_user IS NULL AND t.owner_tenant IS NULL
   AND t.statement_kind <> 'other' AND t.entity_id IS NOT NULL
 ORDER BY t.table_id
"""

_INSERT_SQL = """
INSERT INTO core.fact_table_ref (
  fact_id, table_id, block_id, cell_row, cell_col, page, entity_id, metric_id, period,
  report_value, fact_value, tolerance, unit_scale, period_label, verdict, check_version,
  valid_from, known_at, owner_tenant, owner_user, source, source_ref, ingest_run_id)
SELECT f.fact_id, t.table_id, t.block_id, %(row)s, %(col)s, %(page)s, f.entity_id, f.metric_id,
       f.period, %(report_value)s, f.value, %(tolerance)s, %(unit_scale)s, %(label)s, %(verdict)s,
       %(version)s, f.valid_from, GREATEST(f.known_at, t.known_at), t.owner_tenant, t.owner_user,
       'table_check', %(source_ref)s, %(run_id)s
  FROM core.fin_fact f, core.doc_table t
 WHERE f.fact_id = %(fact_id)s AND t.table_id = %(table_id)s AND t.superseded_at IS NULL
"""


def _units(raw: object) -> tuple[tuple[int, str], ...]:
    if not isinstance(raw, Mapping):
        return ()
    return tuple(sorted((int(k), str(v)) for k, v in raw.items()))


def load_facts(conn: psycopg.Connection[Any]) -> list[FactRow]:
    return [
        FactRow(int(r[0]), str(r[1]), str(r[2]), str(r[3]), Decimal(r[4]), str(r[5]), r[6], r[7])
        for r in conn.execute(_FACTS_SQL).fetchall()
    ]


def load_table_views(conn: psycopg.Connection[Any]) -> dict[str, list[TableView]]:
    by_entity: dict[str, list[TableView]] = {}
    for r in conn.execute(_TABLES_SQL).fetchall():
        entity_id = str(r[2])
        by_entity.setdefault(entity_id, []).append(
            TableView(
                table_id=int(r[0]),
                block_id=int(r[1]),
                doc_period=doc_period_of_title(str(r[3])),
                grid=grid_from_json(r[4]),
                unit=cast("str | None", r[5]),
                unit_source=str(r[6]),
                column_units=_units(r[7]),
                row_units=_units(r[8]),
                currency=cast("str | None", r[9]),
                statement_kind=str(r[10]),
                known_at=r[11],
                caption=cast("str | None", r[12]),
                calendar_fy=True if entity_id.startswith("CN.") else None,
            )
        )
    return by_entity


_RefKey = tuple[int, int, int, int, str, str, Decimal]


def _supersede_refs_of_dead_facts(conn: psycopg.Connection[Any]) -> int:
    """事实已被取代（Wind 更正）而引用还 live 的：引用随事实一起失效。

    取代时刻优先取事实被取代的时刻（as_of 回放时旧事实与它的佐证同时消失）；事实被取代时
    引用还不存在（superseded_at <= 引用 known_at）则用 now()，满足 superseded_at > known_at。
    """
    cur = conn.execute(
        "UPDATE core.fact_table_ref r SET superseded_at ="
        " CASE WHEN f.superseded_at > r.known_at THEN f.superseded_at ELSE now() END"
        " FROM core.fin_fact f WHERE f.fact_id = r.fact_id AND f.superseded_at IS NOT NULL"
        " AND r.superseded_at IS NULL"
    )
    return cur.rowcount


def _write_results(
    conn: psycopg.Connection[Any], results: Sequence[FactCellMatch], *, ingest_run_id: str
) -> tuple[int, int]:
    """幂等写入：某事实现行引用集合 == 期望集合（含 check_version）→ 跳过；否则整体取代再插入。"""
    superseded_dead = _supersede_refs_of_dead_facts(conn)
    live: dict[int, set[_RefKey]] = {}
    for fid, tid, bid, row, col, ver, verdict, fval in conn.execute(
        "SELECT fact_id, table_id, block_id, cell_row, cell_col, check_version, verdict,"
        " fact_value FROM core.fact_table_ref WHERE superseded_at IS NULL"
    ).fetchall():
        live.setdefault(int(fid), set()).add(
            (int(tid), int(bid), int(row), int(col), str(ver), str(verdict), Decimal(fval))
        )
    written, superseded = 0, superseded_dead
    for res in results:
        want: set[_RefKey] = set()
        if res.verdict in WRITTEN_VERDICTS:
            assert res.table_id is not None and res.block_id is not None
            assert res.row is not None and res.col is not None and res.fact_value is not None
            want = {
                (
                    res.table_id,
                    res.block_id,
                    res.row,
                    res.col,
                    FACT_CHECK_VERSION,
                    res.verdict,
                    res.fact_value,
                )
            }
        have = live.get(res.fact_id, set())
        if have == want:
            continue
        if have:
            superseded += conn.execute(
                "UPDATE core.fact_table_ref SET superseded_at = now()"
                " WHERE fact_id = %s AND superseded_at IS NULL",
                (res.fact_id,),
            ).rowcount
        if want:
            cur = conn.execute(
                _INSERT_SQL,
                {
                    "row": res.row,
                    "col": res.col,
                    "page": res.page,
                    "report_value": res.report_value,
                    "tolerance": res.tolerance,
                    "unit_scale": res.unit_scale,
                    "label": res.period_label,
                    "verdict": res.verdict,
                    "version": FACT_CHECK_VERSION,
                    "source_ref": f"{res.block_id}:r{res.row}c{res.col}",
                    "run_id": ingest_run_id,
                    "fact_id": res.fact_id,
                    "table_id": res.table_id,
                },
            )
            written += cur.rowcount
    return written, superseded


def run_fact_table_check(
    conn: psycopg.Connection[Any],
    *,
    ingest_run_id: str,
    write: bool,
    config: FactCheckConfig = FactCheckConfig(),  # noqa: B008
) -> FactCheckReport:
    """校验全部现行 Wind 事实。`write=False` 只读（不碰 `fact_table_ref`，迁移 031 可以还没跑）。"""
    facts = load_facts(conn)
    tables = load_table_views(conn)
    results = [match_fact(f, tables.get(f.entity_id, []), config=config) for f in facts]
    report = FactCheckReport(
        results,
        tables_considered=sum(len(v) for v in tables.values()),
        tables_with_hits=len({r.table_id for r in results if r.table_id is not None}),
    )
    if write:
        report.written, report.superseded = _write_results(
            conn, results, ingest_run_id=ingest_run_id
        )
        conn.commit()
    return report


# ---- 与旧 crosscheck_facts 的对比 ----------------------------------------------------------


@dataclass(frozen=True)
class LegacyComparison:
    legacy_checked: int
    legacy_matched: int
    both_matched: int
    legacy_only: list[tuple[str, str, str, str]]  # (entity, period, metric, 新判定:原因)


def compare_with_legacy(
    conn: psycopg.Connection[Any], results: Sequence[FactCellMatch]
) -> LegacyComparison:
    """旧法（同行最接近的数、不看期别列）判 matched，而新法不是 match 的清单。"""
    new = {(r.entity_id, r.period, r.metric_id): r for r in results}
    legacy = crosscheck_facts(conn)
    legacy_matched = [c for c in legacy if c.matched]
    both = 0
    only: list[tuple[str, str, str, str]] = []
    for c in legacy_matched:
        r = new.get((c.entity_id, c.period, c.metric_id))
        if r is not None and r.verdict == "match":
            both += 1
        else:
            verdict = "missing" if r is None else f"{r.verdict}:{r.reason}"
            only.append((c.entity_id, c.period, c.metric_id, verdict))
    return LegacyComparison(len(legacy), len(legacy_matched), both, only)
