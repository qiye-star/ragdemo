"""人工表格评测题：导出分层的表格标注表（沿用 `human_labels` 的列与导入流程）。

与 `export_label_sheet` 的区别：只导出公告类文档里的表格叶子块，按四个层按配额 2 倍抽候选，
标注员从中挑满配额。`passage` = 表格 Markdown + 页码行，**不含 `content_desc`**：
按描述写出来的题会和检索索引（含 `content_desc`）同源，抬高召回（总计划「评测口径」）。
研究报告（`research_report`）与用户私有上传不导出。
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import psycopg

from ragdemo.evals.human_labels import SHEET_COLUMNS
from ragdemo.evals.sampler import DEFAULT_SEED
from ragdemo.ingest.stage2_crosscheck import FACT_LABELS, normalise_name

TABLE_QUOTA: Final[Mapping[str, int]] = {
    "statement": 20,
    "holders_segments": 15,
    "cross_page_tail": 10,
    "multi_header": 5,
}
_CANDIDATE_FACTOR = 2
MIN_TOTAL_CANDIDATES: Final = 80  # 50 题目标 + 余量；分层候选不足时由 other 层补足
_MIN_ROWS = 3
_MIN_COLS = 2
_DEFAULT_BLOB_ROOT = "data/blob"
_STATEMENT_LABELS = frozenset(
    normalise_name(label) for labels in FACT_LABELS.values() for label in labels
)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S)
_NUMERIC_RE = re.compile(r"^[-+−—–]?[\d,.()（）%\s]+$")


def table_stratum(content: str, *, n_pages: int, header_rows: int) -> str:
    """优先级：cross_page_tail > statement > holders_segments > multi_header > other。"""
    if n_pages > 1:
        return "cross_page_tail"
    for line in content.split("\n"):
        cells = [c.strip() for c in line.split("|")]
        first = next((c for c in cells if c), "")
        if first and normalise_name(first) in _STATEMENT_LABELS:
            return "statement"
    if "持股" in content or "分部" in content:
        return "holders_segments"
    if header_rows >= 2:
        return "multi_header"
    return "other"


def is_usable_other(content: str) -> bool:
    """other 层准入：至少 3 行（含表头）、2 列，且有数字格；太小或纯文字的表出不了数值题。"""
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in content.split("\n")
        if line.strip() and not re.fullmatch(r"[|\s:\-]+", line)
    ]
    if len(rows) < _MIN_ROWS or max((len(r) for r in rows), default=0) < _MIN_COLS:
        return False
    return any(_NUMERIC_RE.match(c) and re.search(r"\d", c) for r in rows for c in r)


def estimate_header_rows(table_html: str | None) -> int:
    """表头行数的推断（TextIn 不给 is_header）：开头连续、不含纯数字格的行；
    之后必须出现含纯数字格的数据行，否则（全文字表）按 1 行。"""
    if not table_html:
        return 1
    for leading, row in enumerate(_ROW_RE.findall(table_html)):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in _CELL_RE.findall(row)]
        if any(c and _NUMERIC_RE.match(c) and re.search(r"\d", c) for c in cells):
            return max(leading, 1)
    return 1


def _pages_by_ordinal(blob_root: Path, ref: str | None, n_blocks: int) -> list[tuple[int, int]]:
    """缓存 JSON 的第 n 张表对应库里第 n 个表块（审计已验证 1:1）；数量对不上或读不到 → 空。
    返回每张表 (总页数, 末页)；回填 `doc_table.pages`（T10）后改读那里。"""
    if not ref:
        return []
    try:
        data: Any = json.loads((blob_root / ref).read_text(encoding="utf-8"))
        detail = data["result"]["detail"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    tables = [d for d in detail if isinstance(d, dict) and d.get("type") == "table"]
    if len(tables) != n_blocks:
        return []
    out: list[tuple[int, int]] = []
    for t in tables:
        extra = [int(p) for p in (t.get("split_section_page_ids") or []) if isinstance(p, int)]
        first = t.get("page_id")
        last = max([int(first), *extra]) if isinstance(first, int) else max(extra, default=0)
        out.append((1 + len(extra), last))
    return out


def export_table_label_sheet(
    conn: psycopg.Connection,
    path: Path,
    *,
    seed: int = DEFAULT_SEED,
    blob_root: Path | None = None,
) -> int:
    root = blob_root or Path(os.environ.get("RAGDEMO_BLOB_ROOT") or _DEFAULT_BLOB_ROOT)
    used = {
        int(b)
        for (arr,) in conn.execute("SELECT gold_block_ids FROM evals.eval_retrieval")
        for b in arr
    }
    rows = conn.execute(
        "SELECT b.block_id, b.doc_id, b.entity_id, d.title, b.page, b.content, b.table_html,"
        " d.parse_json_ref FROM core.doc_block b JOIN core.document d USING (doc_id)"
        " WHERE b.block_type = 'table' AND b.is_leaf AND b.superseded_at IS NULL"
        " AND d.superseded_at IS NULL AND d.doc_type <> 'research_report'"
        " AND b.doc_type <> 'research_report'"
        " AND b.owner_tenant IS NULL AND b.owner_user IS NULL"
        " ORDER BY b.doc_id, b.ordinal"
    ).fetchall()
    by_doc: dict[int, list[Any]] = defaultdict(list)
    for r in rows:
        by_doc[int(r[1])].append(r)

    strata: dict[str, list[tuple[Any, int]]] = defaultdict(list)
    for doc_rows in by_doc.values():
        pages = _pages_by_ordinal(root, doc_rows[0][7], len(doc_rows))
        for i, r in enumerate(doc_rows):
            if int(r[0]) in used:
                continue
            n_pages, last = pages[i] if pages else (1, 0)
            stratum = table_stratum(
                str(r[5]), n_pages=n_pages, header_rows=estimate_header_rows(r[6])
            )
            if stratum == "other" and not is_usable_other(str(r[5])):
                continue
            strata[stratum].append((r, last))

    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    picked: dict[str, list[tuple[Any, int]]] = {}
    for stratum, quota in TABLE_QUOTA.items():
        pool = sorted(strata.get(stratum, []), key=lambda x: int(x[0][0]))
        rng.shuffle(pool)
        picked[stratum] = pool[: quota * _CANDIDATE_FACTOR]
    other = sorted(strata.get("other", []), key=lambda x: int(x[0][0]))
    rng.shuffle(other)
    need = max(0, MIN_TOTAL_CANDIDATES - sum(len(v) for v in picked.values()))
    picked["other"] = other[:need]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(SHEET_COLUMNS)
        for stratum, items in picked.items():
            for r, last in items:
                first = r[4] or ""
                span = f"{first}–{max(int(last), int(first))}" if first and last else f"{first}"
                passage = f"{r[5]}\n页码：{span}"
                w.writerow(
                    [r[0], r[2] or "", f"[{stratum}] {r[3]}", first, passage, "", "", "", ""]
                )
                count += 1
    return count
