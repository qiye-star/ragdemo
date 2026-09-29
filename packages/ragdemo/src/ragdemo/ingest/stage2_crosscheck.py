"""股东数据的独立交叉核对：拿 Wind 入库的前十大股东，去对**已解析的公告原文表格**。

Wind 是股东数据的唯一来源，靠响应内部的一致性校验挡错配，但没有第二来源。这里借阶段 3
已经解析入库的定期报告（季报 / 半年报 / 年报及其摘要，`core.doc_block` 里的表格块）做一次
独立核对：每一名股东，原文表格里必须有一行同时出现（去掉空白与 `<br>` 后）同名、且持股数量
逐股相等。原文没解析（试点之外的实体、年报全文未解析）的期别无从核对，如实报告"无来源"，
不当作通过。

只读库；试点两家之外的实体没有解析过的公告，自然是"无来源"。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg

_KIND_WORDS = {
    "第一季度": "Q1",
    "一季度": "Q1",
    "第三季度": "Q3",
    "三季度": "Q3",
    "半年度": "H1",
    "年度": "FY",
}
_TITLE = re.compile(r"(\d{4})年(第一季度|一季度|第三季度|三季度|半年度|年度)报告(?:摘要)?$")


def normalise_name(name: str) -> str:
    """去掉空白、`<br>` 与"(陆股通)"后缀——解析出的表格常把长名字断行、加空格。"""
    text = name.replace("<br>", "")
    text = re.sub(r"\s", "", text)
    return re.sub(r"[（(]陆股通[)）]", "", text)


def _to_number(cell: str) -> Decimal | None:
    text = re.sub(r"[,\s]", "", cell)
    try:
        return Decimal(text)
    except ArithmeticError:
        return None


def table_rows(contents: list[str]) -> list[list[str]]:
    return [[c.strip() for c in line.split("|")] for text in contents for line in text.split("\n")]


def holder_in_table(name: str, shares: Decimal, rows: list[list[str]]) -> bool:
    target = normalise_name(name)
    return any(
        any(normalise_name(cell) == target for cell in row)
        and any(_to_number(cell) == shares for cell in row)
        for row in rows
    )


@dataclass
class CrosscheckReport:
    checked: int = 0
    matched: int = 0
    mismatched: list[str] = field(default_factory=list)
    no_source: list[str] = field(default_factory=list)


def period_of_title(title: str) -> str | None:
    m = _TITLE.search(title)
    return f"{m.group(1)}{_KIND_WORDS[m.group(2)]}" if m else None


def crosscheck_holders(conn: psycopg.Connection) -> CrosscheckReport:
    report = CrosscheckReport()
    docs: dict[tuple[str, str], list[int]] = {}
    for entity_id, title, doc_id in conn.execute(
        "SELECT entity_id, title, doc_id FROM core.announcement_index"
        " WHERE doc_id IS NOT NULL AND source IN ('cninfo', 'hkexnews')"
    ).fetchall():
        period = period_of_title(str(title))
        if period:
            docs.setdefault((entity_id, period), []).append(int(doc_id))

    held = conn.execute(
        "SELECT entity_id, period, holder_name, shares FROM core.shareholder_holding"
        " WHERE superseded_at IS NULL ORDER BY entity_id, period, rank"
    ).fetchall()
    periods: dict[tuple[str, str], list[tuple[str, Decimal]]] = {}
    for entity_id, period, name, shares in held:
        periods.setdefault((entity_id, period), []).append((str(name), Decimal(shares)))

    for (entity_id, period), holders in periods.items():
        key = f"{entity_id}/{period}"
        contents = [
            str(r[0])
            for doc_id in docs.get((entity_id, period), [])
            for r in conn.execute(
                "SELECT content FROM core.doc_block WHERE doc_id = %s AND content LIKE %s",
                (doc_id, "%持股%"),
            ).fetchall()
        ]
        rows = table_rows(contents)
        # 原文里一名股东都对不上，多半是这份文档根本没有股东表（没解析 / 别的表）——无来源。
        if not any(holder_in_table(n, s, rows) or _name_in(n, rows) for n, s in holders):
            report.no_source.append(key)
            continue
        for name, shares in holders:
            report.checked += 1
            if holder_in_table(name, shares, rows):
                report.matched += 1
            else:
                report.mismatched.append(f"{key} {name} {shares}")
    return report


def _name_in(name: str, rows: list[list[str]]) -> bool:
    target = normalise_name(name)
    return any(any(normalise_name(cell) == target for cell in row) for row in rows)


# --- 财务事实 vs 已解析定期报告原文（阶段 2 出口验收：抽查数字与原文一致，记录页码）----------

# 每个指标在报告表格第一列可能出现的行名（去掉空白与 <br> 后整格相等或以其结尾）。
# 指标 → 行名清单：利润表 / 现金流量表 / 资产负债表科目，与 Wind 适配器的科目一一对应。
FACT_LABELS: dict[str, tuple[str, ...]] = {
    "revenue_total": ("营业收入", "其中:营业收入", "其中：营业收入", "营业总收入"),
    "net_profit_attributable": (
        "归属于上市公司股东的净利润",
        "归属于母公司股东的净利润",
        "归属于母公司所有者的净利润",
        "其中:归属于母公司股东的净利润",
    ),
    "rd_expense": ("研发费用",),
    "operating_cash_flow": ("经营活动产生的现金流量净额",),
    "inventory": ("存货",),
    "contract_liability": ("合同负债",),
    "prepayment": ("预付款项",),
    "capex": ("购建固定资产、无形资产和其他长期资产支付的现金",),
}
# Wind 返回 4 位小数的亿元 / 百万元（约 ±5000 元），不是报表原值——容差取 6000 元。
FACT_TOLERANCE = Decimal(6000)


@dataclass(frozen=True)
class FactCheck:
    entity_id: str
    period: str
    metric_id: str
    fact_value: Decimal
    report_value: Decimal | None  # None = 该文档里找不到这个科目的行
    doc_title: str
    page: int | None

    @property
    def matched(self) -> bool:
        return (
            self.report_value is not None
            and abs(self.fact_value - self.report_value) <= FACT_TOLERANCE
        )


def _label_matches(cell: str, labels: tuple[str, ...]) -> bool:
    text = normalise_name(cell)
    return any(text == normalise_name(label) for label in labels)


def find_report_value(
    blocks: Sequence[tuple[int | None, str]], labels: tuple[str, ...], target: Decimal
) -> tuple[Decimal, int | None] | None:
    """在表格块里找科目行：第一格是行名，同一行的数字格里取与 `target` 最接近的一个。
    返回 (数值, 页码)；行存在但没有数字格返回 None。同一科目在多个块里出现时取最接近的。"""
    best: tuple[Decimal, int | None] | None = None
    for page, content in blocks:
        for line in content.split("\n"):
            cells = [c.strip() for c in line.split("|")]
            if cells and cells[0] == "":
                cells = cells[1:]  # 行首的竖线产生的空格
            if not cells or not _label_matches(cells[0], labels):
                continue
            for cell in cells[1:]:
                number = _to_number(cell)
                if number is None:
                    continue
                if best is None or abs(number - target) < abs(best[0] - target):
                    best = (number, page)
    return best


def crosscheck_facts(conn: psycopg.Connection) -> list[FactCheck]:
    """对每条有已解析原文的 (实体, 期别)，逐指标核对 `fin_fact` 与报告表格里的数。
    `Q2` 是半年报（H1）。原文没解析的期别不产生任何条目（无来源，不是通过）。"""
    docs: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for entity_id, title, doc_id in conn.execute(
        "SELECT entity_id, title, doc_id FROM core.announcement_index"
        " WHERE doc_id IS NOT NULL AND source IN ('cninfo', 'hkexnews')"
    ).fetchall():
        period = period_of_title(str(title))
        if period:
            docs.setdefault((entity_id, period), []).append((int(doc_id), str(title)))

    facts = conn.execute(
        "SELECT entity_id, period, metric_id, value FROM core.fin_fact"
        " WHERE superseded_at IS NULL AND source = 'wind' ORDER BY entity_id, period, metric_id"
    ).fetchall()
    cache: dict[int, list[tuple[int | None, str]]] = {}
    out: list[FactCheck] = []
    for entity_id, period, metric_id, value in facts:
        labels = FACT_LABELS.get(str(metric_id))
        candidates = docs.get((entity_id, str(period).replace("Q2", "H1")), [])
        if labels is None or not candidates:
            continue
        for doc_id, _title in candidates:
            if doc_id not in cache:
                cache[doc_id] = [
                    (None if r[0] is None else int(r[0]), str(r[1]))
                    for r in conn.execute(
                        "SELECT page, content FROM core.doc_block WHERE doc_id = %s"
                        " AND content LIKE '%%|%%'",
                        (doc_id,),
                    ).fetchall()
                ]
        blocks = [b for doc_id, _ in candidates for b in cache[doc_id]]
        if not blocks:
            continue
        fact_value = Decimal(value)
        found = find_report_value(blocks, labels, fact_value)
        title = candidates[0][1]
        out.append(
            FactCheck(
                entity_id,
                str(period),
                str(metric_id),
                fact_value,
                None if found is None else found[0],
                title,
                None if found is None else found[1],
            )
        )
    return out
