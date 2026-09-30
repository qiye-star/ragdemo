"""本地解析 vs TextIn 的对比指标（纯函数）。注意：TextIn 的结果不是绝对标准答案，
指标只说明"两路是否一致"；不一致处由人工抽查裁定（实验报告里记录）。"""

from __future__ import annotations

import re
from collections import Counter

from ragdemo.parse.local.number_closure import numbers_in

_WS = re.compile(r"\s+")


def text_coverage(reference: str, candidate: str) -> float:
    ref = Counter(_WS.sub("", reference))
    if not ref:
        return 1.0
    cand = Counter(_WS.sub("", candidate))
    hit = sum(min(c, cand.get(ch, 0)) for ch, c in ref.items())
    return hit / sum(ref.values())


def number_recall_precision(reference: str, candidate: str) -> tuple[float, float]:
    ref, cand = numbers_in(reference), numbers_in(candidate)
    hit = sum(min(c, cand.get(n, 0)) for n, c in ref.items())
    recall = hit / sum(ref.values()) if ref else 1.0
    precision = hit / sum(cand.values()) if cand else 1.0
    return recall, precision


def _table_rows(md: str) -> list[Counter[str]]:
    """每张表（连续的 `|` 行）跳过第一行（表头），其余行含 >= 2 个数字才算数据行。

    表头里的年份（2024 / 2023）是数字，但不是数据；若把它算进去，"数据行全部串行"
    的表仍会因为表头一致而得到非零召回，指标就骗人了。"""
    rows: list[Counter[str]] = []
    header_pending = True
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            header_pending = True  # 遇到非表格行：下一张表从头开始
            continue
        if set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue  # 分隔行 |---|---|
        if header_pending:
            header_pending = False
            continue
        nums = numbers_in(line)
        if sum(nums.values()) >= 2:
            rows.append(nums)
    return rows


def table_row_recall(reference_md: str, candidate_md: str) -> float:
    ref_rows = _table_rows(reference_md)
    if not ref_rows:
        return 1.0
    cand_rows = _table_rows(candidate_md)
    hit = sum(
        1 for r in ref_rows if any(all(c.get(n, 0) >= k for n, k in r.items()) for c in cand_rows)
    )
    return hit / len(ref_rows)
