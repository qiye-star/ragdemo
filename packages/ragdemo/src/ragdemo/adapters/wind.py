"""万得 Wind 财务数据适配器（2026-09-28 选定为 A 股财务数据源，见 docs/adr/0011）。

经 Wind MCP 的 `get_stock_fundamentals` 取三大报表科目。它是自然语言接口，2026-09-28
实测踩到的坑，每一条都对应下面一道校验：

1. **"累计"还是"单季"不能看列名。** 同样是 `…每季的营业收入` 这种列名，一次返回
   年初至今累计值，另一次返回单季度值（海光 2024Q3：61.3654 亿 vs 23.7363 亿）；问句
   里写明"单季度"，列名有时带"单季度"，有时变成"各期X"（累计）与"各季度X"（单季）。
   因此利润表 / 现金流量表科目**同时要累计值与单季度值**，每个科目必须恰好回两列，
   **按算术判定**哪列是累计：单季度 = 本期累计 − 上期累计（Q1 单季度 = Q1 累计）逐期
   成立的那一种分配才采信；两种都不成立或都成立，整份拒绝。只入库累计值——与数据契约
   "2024Q3 = 年初至三季度末累计"一致。
2. **表结构、列名随问句变。** 列名不照抄问句（问"归属母公司股东的净利润"，列名写
   "归母净利润"；长科目名会被截成"购建固定资产"）；每个科目登记一组可接受的列名
   后缀，出现没登记的数值列整份拒绝。
3. **单位按列各不相同。** 同一份响应里合同负债可能用百万元、其余用亿元。按每列自己的
   `unit` 用 Decimal 换算成元。
4. **披露日期列会出错。** 实测年报那一行的"实际披露日期"是 `Q1 FY2025` 而不是日期；
   格式也在 `2024-04-26` 与 `20240426` 之间变。披露日期解析不了的期别**不入库**
   （没有可信的 known_at），记为警告。

精度：Wind 返回 4 位小数的亿元 / 百万元（约 ±5000 元），不是报表原值；原始响应在
provider_snapshot 里可回放。

时点：`known_at` 取"定期报告实际披露日期"当日 23:59:59（北京时间，公告只有日期精度，
docs/03-point-in-time.md §1.3）。Wind 返回的是**当前**值——某期后来被追溯调整时，
重新取数会看到新值而披露日期不变；`observed_at`（取数时刻）交给写入中间件，把这种
变化记成"在观察到的那一刻才知道的更正"。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import GatewayClient

PROVIDER = "wind"
TOOL = "get_stock_fundamentals"
CST = timezone(timedelta(hours=8))
DEFAULT_QUARTERS = 5  # 日常增量：最近五个季度，迟到的年报也能在下一次取数时补上
SINGLE_QUARTER = "单季度"
# 4 位小数的取整误差：单季度与两期累计之差各自最多差 0.5e-4 个单位，留一点余量。
_TOLERANCE_UNITS = Decimal("0.00025")


class WindResponseRejected(AdapterError):
    """响应的结构、字段、单位或累计 / 单季关系不符合预期，整份不入库。"""


@dataclass(frozen=True)
class WindField:
    """一个要问的科目。`names` 是响应列名可接受的写法（列名以其中之一结尾）。"""

    question: str
    names: tuple[str, ...]
    flow: bool  # True：利润表 / 现金流量表（有累计与单季之分）；False：资产负债表期末值


# 键就是写入 core.metric_source_map 的 provider_field（provider = 'wind'）。
WIND_FIELDS: Mapping[str, WindField] = {
    "营业收入": WindField("营业收入", ("营业收入",), flow=True),
    "归母净利润": WindField(
        "归属母公司股东的净利润", ("归母净利润", "归属母公司股东的净利润"), flow=True
    ),
    "研发费用": WindField("研发费用", ("研发费用",), flow=True),
    "经营活动产生的现金流量净额": WindField(
        "经营活动产生的现金流量净额", ("经营活动产生的现金流量净额",), flow=True
    ),
    "存货": WindField("存货", ("存货",), flow=False),
    "合同负债": WindField("合同负债", ("合同负债",), flow=False),
    "预付款项": WindField("预付款项", ("预付款项",), flow=False),
    "购建固定资产、无形资产和其他长期资产支付的现金": WindField(
        "购建固定资产、无形资产和其他长期资产支付的现金",
        ("购建固定资产、无形资产和其他长期资产支付的现金", "购建固定资产"),
        flow=True,
    ),
}

UNIT_TO_YUAN: Mapping[str, Decimal] = {
    "元": Decimal(1),
    "万元": Decimal(10_000),
    "百万元": Decimal(1_000_000),
    "亿元": Decimal(100_000_000),
}

_PERIOD_LABEL = re.compile(r"^Q([1-4]) FY(\d{4})$")
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_META_PREFIXES = ("Wind代码", "证券简称", "日期", "记账本位币")
_DUPLICATE_SUFFIX = re.compile(r"_\d+$")


def _normalised(name: str) -> str:
    """列名比对前的归一：去掉 Wind 给重名列加的 `_2` 后缀；`_` 当作 `、`
    （实测长电科技的列名把"购建固定资产、无形资产…"写成"购建固定资产_无形资产…"）。"""
    return _DUPLICATE_SUFFIX.sub("", name).replace("_", "、")


def quarter_ends_back(on: date, count: int) -> list[date]:
    """`on` 当天或之前已经结束的最近 `count` 个季度末，升序。"""
    ends: list[date] = []
    year, quarter = on.year, (on.month - 1) // 3 + 1
    while len(ends) < count:
        month, day = _QUARTER_END[quarter]
        end = date(year, month, day)
        if end <= on:
            ends.append(end)
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return sorted(ends)


def full_years(quarter_ends: Sequence[date]) -> list[date]:
    """把区间向前扩到第一年的一季度末：每一期的单季度值都要能用上一期累计值核对。"""
    first, last = min(quarter_ends), max(quarter_ends)
    ends: list[date] = []
    for year in range(first.year, last.year + 1):
        for q in range(1, 5):
            month, day = _QUARTER_END[q]
            end = date(year, month, day)
            if end <= last:
                ends.append(end)
    return ends


def build_question(windcode: str, quarter_ends: Sequence[date], fields: Sequence[str]) -> str:
    """固定模板：列出每个报告期，流量科目同时要累计值与单季度值，再加披露日期。
    结尾的"按报告期逐期列示"不是客套：不加它时沪电股份会回一张期别写在列名里的宽表。"""
    periods = "、".join(d.strftime("%Y%m%d") for d in full_years(quarter_ends))
    flows = [WIND_FIELDS[f].question for f in fields if WIND_FIELDS[f].flow]
    stocks = [WIND_FIELDS[f].question for f in fields if not WIND_FIELDS[f].flow]
    items = [*flows, *(f"{SINGLE_QUARTER}{q}" for q in flows), *stocks]
    return (
        f"{windcode} 报告期{periods}的{'、'.join(items)}，以及各报告期定期报告实际披露日期，"
        "按报告期逐期列示"
    )


def _period_of(label: object) -> tuple[str, date, int, int]:
    match = _PERIOD_LABEL.match(str(label))
    if not match:
        raise WindResponseRejected(f"无法识别的期别标签 {label!r}（期望形如 'Q3 FY2024'）")
    quarter, year = int(match.group(1)), int(match.group(2))
    month, day = _QUARTER_END[quarter]
    period = f"{year}FY" if quarter == 4 else f"{year}Q{quarter}"
    return period, date(year, month, day), year, quarter


def _disclosure_date(cell: object) -> date | None:
    """`2024-04-26` 或 `20240426`；其他任何东西（空、`Q1 FY2025`……）都返回 None。"""
    text = str(cell or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class ParseResult:
    records: list[FactRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Column:
    index: int
    multiplier: Decimal
    name: str

    @property
    def says_single(self) -> bool:
        return SINGLE_QUARTER in self.name


_Rows = Mapping[tuple[int, int], Sequence[Any]]


class WindFundamentalsAdapter:
    """实现 `FactAdapter`：`fetch` 每个实体问一次，`parse` 校验并转成累计口径的 `FactRecord`。"""

    provider = PROVIDER

    def __init__(
        self,
        gateway: GatewayClient,
        *,
        windcodes: Sequence[str],
        fields: Sequence[str] = tuple(WIND_FIELDS),
        quarters: int = DEFAULT_QUARTERS,
    ) -> None:
        unknown = [f for f in fields if f not in WIND_FIELDS]
        if unknown:
            raise ValueError(f"未登记的 Wind 科目：{unknown}")
        self._gateway = gateway
        self._windcodes = list(windcodes)
        self._fields = list(fields)
        self._quarters = quarters

    def health(self) -> bool:
        return self._gateway.health()

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        quarters = int(params.get("quarters", self._quarters))
        ends = quarter_ends_back(ctx.partition_date, quarters)
        for windcode in params.get("windcodes", self._windcodes):
            question = build_question(windcode, ends, self._fields)
            call = self._gateway.call_tool(TOOL, {"question": question})
            yield replace(
                call.raw,
                provider=PROVIDER,
                endpoint=TOOL,
                params={
                    "windcode": windcode,
                    "fields": self._fields,
                    "quarter_ends": [d.isoformat() for d in ends],
                    "question": question,
                },
                payload=call.payload,
            )

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        disclosed = record if isinstance(record, date) else _disclosure_date(record)
        if disclosed is None:
            raise ValueError(f"不是可用的披露日期：{record!r}")
        return datetime.combine(disclosed, time(23, 59, 59), tzinfo=CST)

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        yield from self.parse_with_warnings(raw).records

    def parse_with_warnings(self, raw: RawResponse) -> ParseResult:
        windcode = str(raw.params["windcode"])
        wanted = [str(f) for f in raw.params.get("fields", self._fields)]
        requested = {date.fromisoformat(str(d)) for d in raw.params.get("quarter_ends", [])}
        table = _long_table(raw.payload)
        names = [str(c.get("name", "")) for c in table["columns"]]
        if "日期" not in names:
            raise WindResponseRejected("响应不是逐期长表（缺少'日期'列），整份拒绝")
        columns, disclosure_idx = self._classify(table["columns"], names, wanted)

        rows_by_period: dict[tuple[int, int], list[Any]] = {}
        code_idx, date_idx = names.index("Wind代码"), names.index("日期")
        currency_idx = next(i for i, n in enumerate(names) if n.startswith("记账本位币"))
        for row in table["rows"]:
            if str(row[code_idx]) != windcode:
                raise WindResponseRejected(f"响应里出现了没问的证券 {row[code_idx]!r}")
            _, _, year, quarter = _period_of(row[date_idx])
            rows_by_period[(year, quarter)] = row

        cumulative: dict[str, _Column] = {}
        single: dict[str, _Column] = {}
        for key in wanted:
            if WIND_FIELDS[key].flow:
                cumulative[key], single[key] = _assign(rows_by_period, key, *columns[key])
            else:
                (cumulative[key],) = columns[key]

        result = ParseResult()
        for (year, quarter), row in sorted(rows_by_period.items()):
            period, period_end, _, _ = _period_of(row[date_idx])
            if requested and period_end not in requested:
                continue  # 为了核对单季度才多问的期别，不入库
            disclosed = _disclosure_date(row[disclosure_idx])
            if disclosed is None:
                cell = row[disclosure_idx]
                if cell not in (None, ""):
                    result.warnings.append(
                        f"{windcode} {period}: 披露日期 {cell!r} 无法解析，本期不入库"
                    )
                continue  # 还没披露，或披露日期不可信：没有 known_at 就不入库
            currency = str(row[currency_idx] or "CNY")
            for key in wanted:
                value = _yuan(row, cumulative[key], f"{period} {key}")
                if value is None:
                    continue
                if WIND_FIELDS[key].flow:
                    check = _check(rows_by_period, year, quarter, key, cumulative[key], single[key])
                    if check is None:
                        result.warnings.append(
                            f"{windcode} {period} {key}: 缺少核对累计 / 单季所需的数据，本条不入库"
                        )
                        continue
                result.records.append(
                    FactRecord(
                        entity_ref=windcode,
                        metric_field=key,
                        period=period,
                        period_end=period_end,
                        value=float(value),
                        unit=currency,
                        currency=currency,
                        valid_from=date(year, 1, 1) if WIND_FIELDS[key].flow else period_end,
                        known_at=self.known_at(disclosed),
                        source_ref=f"{windcode}:{period}:{key}",
                        observed_at=raw.fetched_at,
                    )
                )
        return result

    @staticmethod
    def _classify(
        columns: Sequence[Mapping[str, Any]], names: Sequence[str], wanted: Sequence[str]
    ) -> tuple[dict[str, list[_Column]], int]:
        """按科目把数值列分组：资产负债表科目恰好一列，利润表 / 现金流科目恰好两列
        （累计与单季度，谁是谁由 `_assign` 按算术判定，不看列名）。"""
        found: dict[str, list[_Column]] = {k: [] for k in wanted}
        disclosure = [i for i, n in enumerate(names) if "实际披露日期" in n]
        if len(disclosure) != 1:
            raise WindResponseRejected(f"应有且只有一列'实际披露日期'，实际 {len(disclosure)} 列")
        for i, col in enumerate(columns):
            name = names[i]
            if i == disclosure[0] or name.startswith(_META_PREFIXES) or col.get("type") != "number":
                continue
            key = next(
                (
                    k
                    for k in wanted
                    if any(_normalised(name).endswith(n) for n in WIND_FIELDS[k].names)
                ),
                None,
            )
            if key is None:
                raise WindResponseRejected(f"响应里有没问过的数值列 {name!r}")
            unit = str(col.get("unit", ""))
            if unit not in UNIT_TO_YUAN:
                raise WindResponseRejected(f"列 {name!r} 的单位 {unit!r} 不在可换算清单里")
            found[key].append(_Column(i, UNIT_TO_YUAN[unit], name))
        wrong = [
            f"{k}（{len(cols)} 列）"
            for k, cols in found.items()
            if len(cols) != (2 if WIND_FIELDS[k].flow else 1)
        ]
        if wrong:
            raise WindResponseRejected(
                "科目列数不对（利润表 / 现金流科目要累计与单季度两列，资产负债表科目一列）："
                f"{wrong}"
            )
        return found, disclosure[0]


def _check(
    rows: _Rows, year: int, quarter: int, key: str, cum: _Column, single: _Column
) -> str | bool | None:
    """核对这一期 单季度 = 本期累计 − 上期累计（Q1：单季度 = 累计）。
    True = 对得上；None = 缺数据无法核对；字符串 = 对不上，内容是说明。"""
    row = rows[(year, quarter)]
    period = f"{year}FY" if quarter == 4 else f"{year}Q{quarter}"
    this_cum = _yuan(row, cum, f"{period} {key}")
    this_single = _yuan(row, single, f"{period} {key}")
    previous: Decimal | None = Decimal(0)
    if quarter > 1:
        prev_row = rows.get((year, quarter - 1))
        previous = _yuan(prev_row, cum, f"{year}Q{quarter - 1} {key}") if prev_row else None
    if this_cum is None or this_single is None or previous is None:
        return None
    tolerance = _TOLERANCE_UNITS * max(cum.multiplier, single.multiplier)
    if abs(this_single - (this_cum - previous)) <= tolerance:
        return True
    return f"{period} {key}: 单季度 {this_single} ≠ 本期累计 {this_cum} − 上期累计 {previous}"


def _assign(rows: _Rows, key: str, a: _Column, b: _Column) -> tuple[_Column, _Column]:
    """两列里哪一列是累计：把两种假设都逐期核对，恰好一种全部对得上（且至少核对过一期）
    才采信。都不对 = 口径被弄混或数据有误；都对 = 无从区分。两种都整份拒绝。
    列名只作旁证：写着"单季度"的列被算成累计，同样拒绝。"""
    failures: dict[tuple[str, str], list[str]] = {}
    holds: list[tuple[_Column, _Column]] = []
    for cum, single in ((a, b), (b, a)):
        results = [_check(rows, y, q, key, cum, single) for y, q in sorted(rows)]
        bad = [r for r in results if isinstance(r, str)]
        failures[(cum.name, single.name)] = bad
        if not bad and any(r is True for r in results):
            holds.append((cum, single))
    if len(holds) != 1:
        detail = "；".join(
            f"按 {c!r} 为累计：{bad[0] if bad else '全部对得上'}"
            for (c, _), bad in failures.items()
        )
        raise WindResponseRejected(f"{key}: 两列无法唯一判定为 累计 / 单季度（{detail}）——整份拒绝")
    cum, single = holds[0]
    if cum.says_single:
        raise WindResponseRejected(f"{key}: 列 {cum.name!r} 标着单季度，按算术却是累计——整份拒绝")
    return cum, single


def _yuan(row: Sequence[Any] | None, column: _Column, what: str) -> Decimal | None:
    if row is None:
        return None
    cell = row[column.index]
    if cell is None or cell == "":
        return None
    try:
        return Decimal(str(cell)) * column.multiplier
    except InvalidOperation:
        raise WindResponseRejected(f"{what} 的值 {cell!r} 不是数字") from None


def _long_table(payload: Any) -> dict[str, Any]:  # noqa: ANN401
    """取出响应里的逐期长表。Wind 有时把同一批期别拆成几张长表（实测浪潮信息：累计一张、
    单季度一张）；它们按 (Wind代码, 日期) 对齐拼成一张——前提是每张表都是长表、期别集合
    完全一致，否则整份拒绝。"""
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
    if len(tables) == 1:
        return dict(tables[0])
    return _join_on_period(tables)


_KEY_COLUMNS = ("Wind代码", "证券简称", "日期")


def _join_on_period(tables: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keyed: list[tuple[list[int], dict[tuple[str, str], Sequence[Any]]]] = []
    for table in tables:
        names = [str(c.get("name", "")) for c in table["columns"]]
        if not all(k in names for k in ("Wind代码", "日期")):
            raise WindResponseRejected("多张表里有一张不是逐期长表（缺少'Wind代码'或'日期'列）")
        code, when = names.index("Wind代码"), names.index("日期")
        rows = {(str(r[code]), str(r[when])): r for r in table["rows"]}
        if len(rows) != len(table["rows"]):
            raise WindResponseRejected("同一张表里有重复的 (Wind代码, 日期)")
        keep = [i for i, n in enumerate(names) if n not in _KEY_COLUMNS]
        keyed.append((keep, rows))
    periods = set(keyed[0][1])
    if any(set(rows) != periods for _, rows in keyed[1:]):
        raise WindResponseRejected("多张表的期别不一致，无法对齐")
    columns: list[Any] = [{"name": n, "type": "string"} for n in _KEY_COLUMNS]
    for table, (keep, _) in zip(tables, keyed, strict=True):
        columns.extend(table["columns"][i] for i in keep)
    joined = []
    for windcode, label in sorted(periods):
        row: list[Any] = [windcode, None, label]
        for keep, rows in keyed:
            row.extend(rows[(windcode, label)][i] for i in keep)
        joined.append(row)
    return {"columns": columns, "rows": joined}
