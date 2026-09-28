"""万得 Wind 财务数据适配器（2026-09-28 选定为 A 股财务数据源，见 docs/adr/0011）。

经 Wind MCP 的 `get_stock_fundamentals` 取三大报表科目。它是自然语言接口，实测有三个坑：

1. **表结构随问句变。** 问"报告期 A、B 的 X"可能返回宽表（期别写在列名里，如
   `2024年3季报营业总收入`），问"A 到 B 每季的 X"返回长表（每期一行、`日期` 列为
   `Q3 FY2024`）。这里只用固定模板问长表，收到宽表整份拒绝——从自然语言列名里猜期别
   不可靠。
2. **列名不照抄问句。** 问"归属母公司股东的净利润"，列名写的是"归母净利润"。每个字段
   登记一组可接受的名字，响应里出现没登记的数值列同样整份拒绝。
3. **单位按列各不相同。** 同一份响应里合同负债用百万元、其余用亿元。按每列自己的
   `unit` 换算成元，用 Decimal，不在 metric_source_map 里写死一个换算系数。

精度：Wind 返回的是 4 位小数的亿元 / 百万元（约 ±5000 元），不是报表原值；原始响应
在 provider_snapshot 里可回放。

时点：利润表与现金流量表是年初至今累计值（2024Q2 = 1–6 月合计），资产负债表是期末
时点值；`known_at` 取"定期报告实际披露日期"当日 23:59:59（北京时间，公告只有日期精度，
docs/03-point-in-time.md §1.3）。Wind 返回的是**当前**值——若某期后来被追溯调整，
重新取数会看到新值但披露日期不变；`observed_at`（取数时刻）交给写入中间件，把这种变化
记成"在观察到的那一刻才知道的更正"，而不是回填到原披露日。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import GatewayClient

PROVIDER = "wind"
TOOL = "get_stock_fundamentals"
CST = timezone(timedelta(hours=8))
DEFAULT_QUARTERS = 5  # 日常增量：覆盖最近五个季度，迟到的年报也能在下一次取数时补上


class WindResponseRejected(AdapterError):
    """响应的结构、期别、字段或单位不符合预期，整份不入库。"""


@dataclass(frozen=True)
class WindField:
    """一个要问的科目。`names` 是响应列名可接受的写法（列名以其中之一结尾）。"""

    question: str
    names: tuple[str, ...]
    flow: bool  # True：年初至今累计（利润表 / 现金流量表）；False：期末时点（资产负债表）


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
        ("购建固定资产、无形资产和其他长期资产支付的现金",),
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
_META_COLUMNS = {"Wind代码", "证券简称", "日期", "记账本位币"}


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


def _quarter_label(d: date) -> str:
    return f"{d.year}年{(d.month - 1) // 3 + 1}季末"


def build_question(windcode: str, quarter_ends: Sequence[date], fields: Sequence[str]) -> str:
    """固定模板：问"从某季末到某季末每季的若干科目"，让 Wind 返回长表。"""
    first, last = quarter_ends[0], quarter_ends[-1]
    items = "、".join(WIND_FIELDS[f].question if f in WIND_FIELDS else f for f in fields)
    return (
        f"{windcode} {_quarter_label(first)}到{_quarter_label(last)}每季的{items}，"
        "以及每季定期报告实际披露日期"
    )


def _period_of(label: object) -> tuple[str, date]:
    match = _PERIOD_LABEL.match(str(label))
    if not match:
        raise WindResponseRejected(f"无法识别的期别标签 {label!r}（期望形如 'Q3 FY2024'）")
    quarter, year = int(match.group(1)), int(match.group(2))
    month, day = _QUARTER_END[quarter]
    return (f"{year}FY" if quarter == 4 else f"{year}Q{quarter}"), date(year, month, day)


def _field_of(column_name: str, wanted: Sequence[str]) -> str | None:
    for key in wanted:
        if any(column_name.endswith(n) for n in WIND_FIELDS[key].names):
            return key
    return None


class WindFundamentalsAdapter:
    """实现 `FactAdapter`：`fetch` 每个实体问一次，`parse` 把长表转成 `FactRecord`。"""

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
        disclosed = record if isinstance(record, date) else date.fromisoformat(str(record))
        return datetime.combine(disclosed, time(23, 59, 59), tzinfo=CST)

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        windcode = str(raw.params["windcode"])
        wanted = [str(f) for f in raw.params.get("fields", self._fields)]
        table = self._single_table(raw.payload)
        columns: list[dict[str, Any]] = table["columns"]
        names = [str(c.get("name", "")) for c in columns]
        if "日期" not in names:
            raise WindResponseRejected(
                "响应不是逐期长表（缺少'日期'列）——期别写在列名里的宽表不可靠，整份拒绝"
            )

        disclosure_idx = [i for i, n in enumerate(names) if "实际披露日期" in n]
        if len(disclosure_idx) != 1:
            raise WindResponseRejected(
                f"应有且只有一列'实际披露日期'，实际 {len(disclosure_idx)} 列"
            )

        field_columns: dict[str, tuple[int, Decimal]] = {}
        for i, col in enumerate(columns):
            name = names[i]
            if name in _META_COLUMNS or i in disclosure_idx or col.get("type") != "number":
                continue
            key = _field_of(name, wanted)
            if key is None:
                raise WindResponseRejected(f"响应里有没问过的数值列 {name!r}")
            unit = str(col.get("unit", ""))
            if unit not in UNIT_TO_YUAN:
                raise WindResponseRejected(f"列 {name!r} 的单位 {unit!r} 不在可换算清单里")
            field_columns[key] = (i, UNIT_TO_YUAN[unit])
        missing = [f for f in wanted if f not in field_columns]
        if missing:
            raise WindResponseRejected(f"响应缺少请求的科目：{missing}")

        date_idx, currency_idx = names.index("日期"), names.index("记账本位币")
        code_idx = names.index("Wind代码")
        for row in table["rows"]:
            if str(row[code_idx]) != windcode:
                raise WindResponseRejected(f"响应里出现了没问的证券 {row[code_idx]!r}")
            period, period_end = _period_of(row[date_idx])
            disclosed = row[disclosure_idx[0]]
            if disclosed in (None, ""):
                continue  # 还没披露：不可能"已知"
            known_at = self.known_at(disclosed)
            currency = str(row[currency_idx] or "CNY")
            for key, (idx, multiplier) in field_columns.items():
                cell = row[idx]
                if cell is None:
                    continue
                try:
                    yuan = Decimal(str(cell)) * multiplier
                except InvalidOperation:
                    raise WindResponseRejected(f"{period} {key} 的值 {cell!r} 不是数字") from None
                yield FactRecord(
                    entity_ref=windcode,
                    metric_field=key,
                    period=period,
                    period_end=period_end,
                    value=float(yuan),
                    unit=currency,
                    currency=currency,
                    valid_from=date(period_end.year, 1, 1) if WIND_FIELDS[key].flow else period_end,
                    known_at=known_at,
                    source_ref=f"{windcode}:{period}:{key}",
                    observed_at=raw.fetched_at,
                )

    @staticmethod
    def _single_table(payload: Any) -> dict[str, Any]:  # noqa: ANN401
        if not isinstance(payload, Mapping) or payload.get("error"):
            error = payload.get("error") if isinstance(payload, Mapping) else payload
            raise WindResponseRejected(f"Wind 返回错误或空响应：{error!r}")
        data = payload.get("data")
        tables = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(tables, list) or len(tables) != 1:
            raise WindResponseRejected(f"期望恰好一张表，实际 {tables!r:.200}")
        table = tables[0]
        if not isinstance(table, Mapping) or "columns" not in table or "rows" not in table:
            raise WindResponseRejected("表缺少 columns / rows")
        return dict(table)
