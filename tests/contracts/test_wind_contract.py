"""WindFundamentalsAdapter 通过全部适配器契约。

用一个按问句"现场作答"的假网关：它从问句里读出报告期，只返回那些期别、披露日期
取季末后 40 天——这样"回填两年前的分区时 known_at 仍在历史区间"这条契约是真在
检验适配器的取数区间，而不是在回放一份固定的夹具。为了核对单季度，问句会把区间
向前补到第一年一季度，多出的期别不入库。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from ragdemo.adapters.base import Adapter, RawResponse
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind import WIND_FIELDS, WindFundamentalsAdapter
from tests.contracts.adapter_contract import AdapterContract

_PERIODS = re.compile(r"报告期([\d、]+)的")


def _answer(question: str) -> dict[str, Any]:
    """按问句列出的报告期作答：累计值 = 季度序号，单季度值恒为 1，两者自洽。"""
    match = _PERIODS.search(question)
    assert match is not None, question
    ends = [datetime.strptime(d, "%Y%m%d").date() for d in match.group(1).split("、")]
    flows = [f for f in WIND_FIELDS.values() if f.flow]
    stocks = [f for f in WIND_FIELDS.values() if not f.flow]
    columns = [
        {"name": "Wind代码", "type": "string"},
        {"name": "证券简称", "type": "string"},
        {"name": "日期", "type": "string"},
        {"name": "每期定期报告实际披露日期", "type": "date"},
        {"name": "记账本位币", "type": "string"},
        *({"name": f"每期{f.names[0]}", "type": "number", "unit": "亿元"} for f in flows),
        *({"name": f"每期单季度{f.names[0]}", "type": "number", "unit": "亿元"} for f in flows),
        *({"name": f"每期{f.names[0]}", "type": "number", "unit": "亿元"} for f in stocks),
    ]
    rows = []
    for e in ends:
        quarter = (e.month - 1) // 3 + 1
        rows.append(
            [
                "688041.SH",
                "海光信息",
                f"Q{quarter} FY{e.year}",
                (e + timedelta(days=40)).strftime("%Y%m%d"),
                "CNY",
                *([float(quarter)] * len(flows)),
                *([1.0] * len(flows)),
                *([1.0] * len(stocks)),
            ]
        )
    return {"data": {"data": [{"columns": columns, "rows": rows}]}, "error": None}


class _AnsweringGateway(MockMcpGateway):
    def __init__(self) -> None:
        super().__init__(tools=[ToolSpec("get_stock_fundamentals", "", {})])

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:  # noqa: ANN401
        self._responses[name] = _answer(str(arguments["question"]))
        return super().call_tool(name, arguments)


class TestWindAdapterContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return WindFundamentalsAdapter(_AnsweringGateway(), windcodes=["688041.SH"])

    def known_at_inputs(self, raw: RawResponse) -> list[object]:
        table = raw.payload["data"]["data"][0]
        idx = next(i for i, c in enumerate(table["columns"]) if "实际披露日期" in c["name"])
        return [row[idx] for row in table["rows"]]


def test_parse_of_an_answered_question_yields_records() -> None:
    adapter = WindFundamentalsAdapter(_AnsweringGateway(), windcodes=["688041.SH"], quarters=4)
    from ragdemo.adapters.base import FetchContext

    raws = list(adapter.fetch(FetchContext(ingest_run_id="t", partition_date=date(2024, 10, 28))))
    records = [r for raw in raws for r in adapter.parse(raw)]
    assert {r.period for r in records} == {"2023FY", "2024Q1", "2024Q2", "2024Q3"}
