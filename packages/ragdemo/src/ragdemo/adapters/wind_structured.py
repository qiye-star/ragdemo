"""万得 Wind：前十大股东与主营构成（阶段二 Task 5/6）。

同一个自然语言接口族（`get_stock_equity_holders` / `get_stock_fundamentals`），
2026-09-29 用两家试点实录（`tests/fixtures/wind/holders_*`、`segments_*`）确认的事实：

- **股东可取历史期**：问句写"2024年年报(报告期2024-12-31)"，列名变成
  `2024年前十大股东名称 / 持股数量 / 持股比例`（单位 股、%）加一列 `名次`。列名只到"年"、
  不到季度，所以**只有年报（FY）接受"仅年份"的列名**；其它期别要求列名里带"中报/一季报/
  三季报"字样且与请求一致，对不上整份拒绝。问"最新报告期"时列名是 `最新一期…`、不带期别，
  这里不用。因为期别主要靠问句保证，再加一道**一致性校验**：各行 `股数 ÷ 比例` 隐含的总股本
  必须一致（比例只有两位小数，容差按每行的取整误差算），挡住混期。
- **分部响应带期别**：列名以 `2024年年报…` / `2025年中报…` 开头，必须与请求期别一致；一份
  响应有多张表（产品、地区），维度由列名里的 `主营产品` / `主营地区` / `主营行业` 区分；每列
  单位各异（亿元 / 百万元），按列换算成元；行数补空到名次 10（名称为 null 的行丢弃）；
  中报问法只回 `前5大…`（不保证完整）。`毛利占比`、`毛利计算值`、`总毛利` 等派生列口径
  混乱（浪潮出现负数），**一律不读**，只取 名称 / 收入 / 毛利 / 收入占比。
  收入之和必须与响应里自带的总收入列（`营业总收入` 或 `主营XX总收入`）吻合，
  每行占比必须与 收入÷总收入 吻合——不吻合整份拒绝。

Wind 返回的是**当前值**，不给披露日：`known_at` 由调用方按公告目录回填
（`ingest/period_disclosure.py`）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.adapters.structured_rows import HoldingRow, SegmentRow
from ragdemo.adapters.wind import UNIT_TO_YUAN, WindResponseRejected
from ragdemo.ingest.period_disclosure import period_end_of

PROVIDER = "wind"
HOLDERS_TOOL = "get_stock_equity_holders"
FUNDAMENTALS_TOOL = "get_stock_fundamentals"

# 期别 → Wind 列名里的报告名。
_REPORT_WORD = {"FY": "年报", "H1": "中报", "Q1": "一季报", "Q3": "三季报"}
_PERIOD = re.compile(r"^(\d{4})(FY|H1|Q1|Q3)$")
_DIMENSIONS = {"产品": "product", "地区": "region", "行业": "industry"}

_RATIO_TOLERANCE = Decimal("0.001")  # 隐含总股本一致性的额外容差（0.1%）
_SHARE_TOLERANCE = Decimal("0.001")  # 分部占比与 收入÷总收入 的容差（0.1 个百分点）
_TOTAL_TOLERANCE = Decimal("0.005")  # 分部收入之和与总收入的相对容差（0.5%）
_PERCENT_SUM_LIMIT = Decimal("100.1")


def _split_period(period: str) -> tuple[str, str]:
    m = _PERIOD.match(period)
    if m is None:
        raise ValueError(f"不支持的期别 {period!r}")
    return m.group(1), m.group(2)


def period_phrase(period: str) -> str:
    """问句里的期别写法，如 `2024年年报(报告期2024-12-31)`。"""
    year, kind = _split_period(period)
    return f"{year}年{_REPORT_WORD[kind]}(报告期{period_end_of(period).isoformat()})"


def _tables(payload: Any) -> list[Mapping[str, Any]]:  # noqa: ANN401
    if not isinstance(payload, Mapping) or payload.get("error"):
        error = payload.get("error") if isinstance(payload, Mapping) else payload
        raise WindResponseRejected(f"Wind 返回错误或空响应：{error!r}")
    data = payload.get("data")
    tables = data.get("data") if isinstance(data, Mapping) else None
    if not isinstance(tables, list) or not tables:
        raise WindResponseRejected(f"响应里没有表：{tables!r:.200}")
    for table in tables:
        if not isinstance(table, Mapping) or "columns" not in table or "rows" not in table:
            raise WindResponseRejected("表缺少 columns / rows")
    return list(tables)


def _number(cell: Any, what: str) -> Decimal | None:  # noqa: ANN401
    if cell is None or cell == "":
        return None
    try:
        return Decimal(str(cell))
    except InvalidOperation:
        raise WindResponseRejected(f"{what} 的值 {cell!r} 不是数字") from None


def _multiplier(column: Mapping[str, Any]) -> Decimal:
    unit = str(column.get("unit", ""))
    if unit not in UNIT_TO_YUAN:
        raise WindResponseRejected(f"列 {column.get('name')!r} 的单位 {unit!r} 无法识别")
    return UNIT_TO_YUAN[unit]


def _column_index(columns: Sequence[Mapping[str, Any]], pattern: re.Pattern[str], what: str) -> int:
    hits = [i for i, c in enumerate(columns) if pattern.match(str(c.get("name", "")))]
    if len(hits) != 1:
        raise WindResponseRejected(f"{what}：应恰有一列匹配 {pattern.pattern}，实际 {len(hits)} 列")
    return hits[0]


# --------------------------------------------------------------------------- 股东


@dataclass(frozen=True)
class HoldersParse:
    period: str
    rows: list[HoldingRow]


class WindHoldersAdapter:
    provider = PROVIDER

    def __init__(self, gateway: GatewayClient) -> None:
        self._gateway = gateway

    def health(self) -> bool:
        return self._gateway.health()

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        windcode, name, period = str(params["windcode"]), str(params["name"]), str(params["period"])
        question = f"查询{name}({windcode}){period_phrase(period)}的前十大股东、持股数量和持股比例"
        call = self._gateway.call_tool(HOLDERS_TOOL, {"question": question})
        yield replace(
            call.raw,
            provider=PROVIDER,
            endpoint=HOLDERS_TOOL,
            params={"windcode": windcode, "period": period, "question": question},
            payload=call.payload,
        )

    def known_at(self, record: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError("股东数据的 known_at 由公告目录回填（period_disclosure）")

    def parse(self, raw: RawResponse) -> HoldersParse:
        windcode, period = str(raw.params["windcode"]), str(raw.params["period"])
        year, kind = _split_period(period)
        tables = _tables(raw.payload)
        if len(tables) != 1:
            raise WindResponseRejected(f"股东响应应恰有一张表，实际 {len(tables)} 张")
        table = tables[0]
        columns = table["columns"]
        # 年报允许列名只写年份；其它期别必须写明报告名且与请求一致。
        word = _REPORT_WORD[kind]
        label = rf"{year}年{word}" if kind != "FY" else rf"{year}年(?:{word})?"

        def col(suffix: str) -> int:
            return _column_index(
                columns, re.compile(rf"^{label}前十大股东{suffix}$"), f"股东{suffix}"
            )

        name_i, shares_i, ratio_i = col("名称"), col("持股数量"), col("持股比例")
        rank_i = _column_index(columns, re.compile(r"^名次$"), "名次")
        code_i = _column_index(columns, re.compile(r"^Wind代码$"), "Wind代码")
        shares_mult, ratio_unit = (
            _multiplier_shares(columns[shares_i]),
            columns[ratio_i].get("unit"),
        )
        if ratio_unit != "%":
            raise WindResponseRejected(f"持股比例的单位应为 %，实际 {ratio_unit!r}")

        rows: list[HoldingRow] = []
        percents: list[Decimal] = []
        for r in table["rows"]:
            if str(r[code_i]) != windcode:
                raise WindResponseRejected(f"响应里出现了没问的证券 {r[code_i]!r}")
            holder = r[name_i]
            if holder is None or holder == "":
                continue  # 名次补空行
            shares = _number(r[shares_i], "持股数量")
            pct = _number(r[ratio_i], "持股比例")
            rank = _number(r[rank_i], "名次")
            if shares is None or pct is None or rank is None or shares <= 0 or pct <= 0:
                raise WindResponseRejected(f"{holder}：持股数量/比例缺失或非正数")
            percents.append(pct)
            rows.append(HoldingRow(int(rank), str(holder), shares * shares_mult, pct / 100))
        _check_holders(rows, percents)
        return HoldersParse(period=period, rows=rows)


def _multiplier_shares(column: Mapping[str, Any]) -> Decimal:
    unit = str(column.get("unit", ""))
    if unit == "股":
        return Decimal(1)
    if unit == "万股":
        return Decimal(10_000)
    raise WindResponseRejected(f"持股数量的单位 {unit!r} 无法识别")


def _check_holders(rows: Sequence[HoldingRow], percents: Sequence[Decimal]) -> None:
    if not rows:
        raise WindResponseRejected("没有任何股东行")
    if [r.rank for r in rows] != list(range(1, len(rows) + 1)) or len(rows) > 10:
        raise WindResponseRejected(f"名次不连续或超过 10：{[r.rank for r in rows]}")
    if sum(percents) > _PERCENT_SUM_LIMIT:
        raise WindResponseRejected(f"前十大持股比例之和 {sum(percents)}% 超过 100%")
    if any(a.shares < b.shares for a, b in pairwise(rows)):
        raise WindResponseRejected("持股数量没有按名次递减，疑似混入了不同报告期的数据")
    # 隐含总股本一致性：以名次 1（比例最大、取整误差最小）为基准。
    ref = rows[0].shares / (percents[0] / 100)
    for row, pct in zip(rows, percents, strict=True):
        implied = row.shares / (pct / 100)
        allowed = Decimal("0.005") / pct + _RATIO_TOLERANCE + Decimal("0.005") / percents[0]
        if abs(implied - ref) / ref > allowed:
            raise WindResponseRejected(
                f"{row.holder_name}：隐含总股本 {implied:.0f} 与名次 1 的 {ref:.0f} 不一致，"
                "疑似混期"
            )


# --------------------------------------------------------------------------- 分部


class WindSegmentsAdapter:
    provider = PROVIDER

    def __init__(self, gateway: GatewayClient) -> None:
        self._gateway = gateway

    def health(self) -> bool:
        return self._gateway.health()

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        windcode, name, period = str(params["windcode"]), str(params["name"]), str(params["period"])
        _, kind = _split_period(period)
        if kind not in ("FY", "H1"):
            raise ValueError("分部只在年报 / 中报披露，不支持季报期别")
        question = (
            f"查询{name}({windcode}){period_phrase(period)}按产品和按地区划分的"
            "主营业务收入、毛利和收入占比"
        )
        call = self._gateway.call_tool(FUNDAMENTALS_TOOL, {"question": question})
        yield replace(
            call.raw,
            provider=PROVIDER,
            endpoint=FUNDAMENTALS_TOOL,
            params={"windcode": windcode, "period": period, "question": question},
            payload=call.payload,
        )

    def known_at(self, record: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError("分部数据的 known_at 由公告目录回填（period_disclosure）")

    def parse(self, raw: RawResponse) -> list[SegmentRow]:
        windcode, period = str(raw.params["windcode"]), str(raw.params["period"])
        year, kind = _split_period(period)
        if kind not in ("FY", "H1"):
            raise WindResponseRejected(f"分部不支持期别 {period}")
        prefix = rf"{year}年{_REPORT_WORD[kind]}(?:前\d+大)?"
        out: list[SegmentRow] = []
        seen_dims: set[str] = set()
        for table in _tables(raw.payload):
            columns = table["columns"]
            dim = _dimension_of(columns, prefix)
            if dim in seen_dims:
                raise WindResponseRejected(f"维度 {dim} 出现了不止一张表")
            seen_dims.add(dim)
            out.extend(_parse_segment_table(table, prefix, dim, windcode))
        if not out:
            raise WindResponseRejected("没有解析出任何分部行")
        return out


def _dimension_of(columns: Sequence[Mapping[str, Any]], prefix: str) -> str:
    found = {
        _DIMENSIONS[m.group(1)]
        for c in columns
        if (m := re.match(rf"^{prefix}主营(产品|地区|行业)名称$", str(c.get("name", ""))))
    }
    if len(found) != 1:
        raise WindResponseRejected(
            f"无法确定维度，或列名期别与请求不符（应以 {prefix} 开头）：{sorted(found)}"
        )
    return next(iter(found))


def _parse_segment_table(
    table: Mapping[str, Any], prefix: str, dim: str, windcode: str
) -> list[SegmentRow]:
    columns = table["columns"]
    word = {"product": "产品", "region": "地区", "industry": "行业"}[dim]

    def col(pattern: str, what: str) -> int:
        return _column_index(columns, re.compile(f"^{prefix}{pattern}$"), what)

    name_i = col(f"主营{word}名称", "名称")
    rev_i = col(f"主营{word}收入", "收入")
    share_i = col(f"主营{word}收入占比", "收入占比")
    gp_hits = [
        i for i, c in enumerate(columns) if re.match(f"^{prefix}主营{word}毛利$", str(c["name"]))
    ]
    gp_i = gp_hits[0] if len(gp_hits) == 1 else None
    code_i = _column_index(columns, re.compile(r"^Wind代码$"), "Wind代码")
    rev_mult = _multiplier(columns[rev_i])
    gp_mult = _multiplier(columns[gp_i]) if gp_i is not None else None
    if columns[share_i].get("unit") != "%":
        raise WindResponseRejected("收入占比的单位应为 %")

    # 总收入列：`主营XX总收入`（前 N 大之和）或 `营业总收入`；两者取有的，都没有就无法对账。
    total_i = next(
        (
            i
            for i, c in enumerate(columns)
            if re.match(rf"^{prefix}(?:主营{word}总收入|营业总收入)$", str(c["name"]))
        ),
        None,
    )

    rows: list[SegmentRow] = []
    total: Decimal | None = None
    for r in table["rows"]:
        if str(r[code_i]) != windcode:
            raise WindResponseRejected(f"响应里出现了没问的证券 {r[code_i]!r}")
        if total_i is not None and total is None:
            t = _number(r[total_i], "总收入")
            total = t * _multiplier(columns[total_i]) if t is not None else None
        seg = r[name_i]
        if seg is None or seg == "":
            continue
        revenue = _number(r[rev_i], f"{seg} 收入")
        if revenue is None:
            raise WindResponseRejected(f"{seg}：有名称但没有收入")
        gross = _number(r[gp_i], f"{seg} 毛利") if gp_i is not None else None
        pct = _number(r[share_i], f"{seg} 收入占比")
        rows.append(
            SegmentRow(
                dimension=dim,
                name=str(seg),
                revenue=revenue * rev_mult,
                gross_profit=gross * gp_mult if gross is not None and gp_mult is not None else None,
                share=pct / 100 if pct is not None else None,
            )
        )
    names = [s.name for s in rows]
    if len(set(names)) != len(names):
        raise WindResponseRejected(f"{dim}：分部名称重复 {names}")
    _check_segments(rows, total, dim)
    return rows


def _check_segments(rows: Sequence[SegmentRow], total: Decimal | None, dim: str) -> None:
    if not rows:
        return
    if total is None or total <= 0:
        raise WindResponseRejected(f"{dim}：响应没有可用于对账的总收入列，整份拒绝")
    listed = sum(s.revenue for s in rows)
    if abs(listed - total) / total > _TOTAL_TOLERANCE:
        raise WindResponseRejected(
            f"{dim}：分部收入之和 {listed} 与总收入 {total} 相差超过 {_TOTAL_TOLERANCE:.1%}"
        )
    for s in rows:
        if s.share is not None and abs(s.revenue / total - s.share) > _SHARE_TOLERANCE:
            raise WindResponseRejected(
                f"{dim}/{s.name}：占比 {s.share} 与 收入÷总收入 {s.revenue / total:.5f} 不吻合"
            )


def check_against_revenue(
    rows: Sequence[SegmentRow],
    total_revenue: Decimal,
    *,
    tolerance: Decimal = Decimal("0.005"),
) -> None:
    """与 `fin_fact` 该期营业收入对账：每个维度的收入之和不得**超过**营业收入（抵销后
    分部和可以略大于合并营收，故给 `tolerance` 的上浮空间），且不得远小于（完整披露
    的维度应接近合并营收；中报 `前 N 大` 可能不全，调用方按需放宽）。"""
    if total_revenue <= 0:
        raise WindResponseRejected(f"营业收入 {total_revenue} 非正数，无法对账")
    for dim in {r.dimension for r in rows}:
        listed = sum(r.revenue for r in rows if r.dimension == dim)
        if abs(listed - total_revenue) / total_revenue > tolerance:
            raise WindResponseRejected(
                f"{dim}：分部收入之和 {listed} 与营业收入 {total_revenue} 相差超过 {tolerance:.1%}"
            )
