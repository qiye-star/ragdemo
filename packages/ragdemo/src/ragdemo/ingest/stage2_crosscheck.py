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
