"""WindFundamentalsAdapter 通过全部适配器契约。

用一个按问句"现场作答"的假网关：它从问句里读出季度区间，只返回那些季度、披露日期
取季末后 40 天——这样"回填两年前的分区时 known_at 仍在历史区间"这条契约是真在
检验适配器的取数区间，而不是在回放一份固定的夹具。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from ragdemo.adapters.base import Adapter, RawResponse
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind import WIND_FIELDS, WindFundamentalsAdapter, quarter_ends_back
from tests.contracts.adapter_contract import AdapterContract

_RANGE = re.compile(r"(\d{4})年([1-4])季末到(\d{4})年([1-4])季末")


def _answer(question: str) -> dict[str, Any]:
    y0, q0, y1, q1 = (int(g) for g in _RANGE.search(question).groups())  # type: ignore[union-attr]
    last = date(y1, (q1 * 3), 30 if q1 in (2, 3) else 31)
    count = (y1 - y0) * 4 + (q1 - q0) + 1
    ends = quarter_ends_back(last, count)
    columns = [
        {"name": "Wind代码", "type": "string"},
        {"name": "证券简称", "type": "string"},
        {"name": "日期", "type": "string"},
        {"name": "每季定期报告实际披露日期", "type": "date"},
        {"name": "记账本位币", "type": "string"},
        *(
            {"name": f"每季的{names.names[0]}", "type": "number", "unit": "亿元"}
            for names in WIND_FIELDS.values()
        ),
    ]
    rows = [
        [
            "688041.SH",
            "海光信息",
            f"Q{(e.month - 1) // 3 + 1} FY{e.year}",
            (e + timedelta(days=40)).isoformat(),
            "CNY",
            *([1.0] * len(WIND_FIELDS)),
        ]
        for e in ends
    ]
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
