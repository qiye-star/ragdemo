"""财务报表页识别：命中即强制 L3（TextIn）。宁可误判多花钱，不可漏判。"""

from __future__ import annotations

import re

from ragdemo.parse.local.number_closure import numbers_in

# "合并资产负债表""母公司利润表"等都包含这些子串。
FINANCIAL_STATEMENT_TITLES: tuple[str, ...] = (
    "资产负债表",
    "利润表",
    "现金流量表",
    "所有者权益变动表",
    "股东权益变动表",
    "分部信息",
    "报告分部",
)
MIN_NUMBERS_ON_STATEMENT_PAGE = 20  # 报表页数字密集；正文里顺带提到报表名不会触发


def is_financial_statement_page(text: str) -> bool:
    flat = re.sub(r"\s+", "", text)
    if not any(t in flat for t in FINANCIAL_STATEMENT_TITLES):
        return False
    return sum(numbers_in(text).values()) >= MIN_NUMBERS_ON_STATEMENT_PAGE
