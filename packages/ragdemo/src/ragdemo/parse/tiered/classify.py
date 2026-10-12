"""财务报表页识别：命中即强制 L3（TextIn）。宁可误判多花钱，不可漏判。

两条触发规则（都要求数字密集，正文里顺带提到报表名不会触发）：
1. 命中报表标题 且 数字 >= MIN_NUMBERS_ON_STATEMENT_PAGE；
2. 命中至少 MIN_ROW_MARKERS 个不同的报表行标记（续页没有标题时靠它）且数字 >= 同一阈值。"""

from __future__ import annotations

import re

from ragdemo.parse.local.number_closure import numbers_in

# "合并资产负债表""母公司利润表"等都包含这些子串；繁体与英文一并覆盖（英文匹配忽略大小写与空白）。
FINANCIAL_STATEMENT_TITLES: tuple[str, ...] = (
    "资产负债表",
    "利润表",
    "损益表",
    "综合收益表",
    "现金流量表",
    "所有者权益变动表",
    "股东权益变动表",
    "分部信息",
    "报告分部",
    "資產負債表",
    "利潤表",
    "損益表",
    "綜合收益表",
    "現金流量表",
    "所有者權益變動表",
    "股東權益變動表",
    "Consolidated Balance Sheet",
    "Balance Sheet",
    "Statement of Financial Position",
    "Statement of Cash Flow",
    "Statements of Cash Flow",
    "Statement of Comprehensive Income",
    "Statements of Comprehensive Income",
    "Income Statement",
)
# 报表行/列标记：不依赖标题，续页上也会出现。
STATEMENT_ROW_MARKERS: tuple[str, ...] = (
    "流动资产合计",
    "负债和所有者权益总计",
    "经营活动产生的现金流量净额",
    "本期金额",
    "上期金额",
    "期末余额",
    "期初余额",
)
MIN_NUMBERS_ON_STATEMENT_PAGE = 20
MIN_ROW_MARKERS = 2


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def is_financial_statement_page(text: str) -> bool:
    flat = _flat(text)
    has_title = any(_flat(t) in flat for t in FINANCIAL_STATEMENT_TITLES)
    markers = sum(1 for m in STATEMENT_ROW_MARKERS if m in flat)
    if not has_title and markers < MIN_ROW_MARKERS:
        return False
    return sum(numbers_in(text).values()) >= MIN_NUMBERS_ON_STATEMENT_PAGE
