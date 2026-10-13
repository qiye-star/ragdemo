"""只读测量减页规则（L0 + TextIn 分页接入计划 T2）：积压候选页、扣除 L0 重叠后的增量、
31 份现行 TextIn 文档上的验证证据与门槛判定，外加矢量描边文字 / 墨迹像素探测与 D1-D5 文档级候选。

不调任何付费接口、不写库；只打印计数与 doc_id:page，不打印正文与环境变量。
用法：set -a && . ./.env && set +a && uv run --no-sync python scripts/skip_audit.py
"""

from __future__ import annotations

import glob
import os
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pypdfium2 as pdfium
from pypdf import PdfReader

from ragdemo.config import DEFAULT_TEXTIN_COST_PER_PAGE_CNY, load_config
from ragdemo.ingest.parse_pending import tier_for_document
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.parse.local.l0 import parse_layouts_l0
from ragdemo.parse.local.layout import extract_pages
from ragdemo.parse.tiered.census import force_whole_document_l3
from ragdemo.parse.tiered.skip import DocKind, evaluate_skip_rules, non_date_numbers, page_facts
from ragdemo.parse.tiered.skip_audit import (
    INK_RATIO_REVIEW,
    BacklogCandidate,
    SkipEvidence,
    drawing_group,
    ink_ratio,
    judge_rule,
    savings,
    summary_base,
)
from ragdemo.parse.tiered.types import SKIP_RULE_IDS, SkipRuleId
from ragdemo_core.db.session import as_of_session

Row = tuple[Any, ...]
Item = tuple[str, DocKind, str]  # (键, 文档口径, PDF 路径)
_SIGN = re.compile(r"董事会|监事会|特此公告")
RENDER_SCALE = 100 / 72


@dataclass
class DocResult:
    n_pages: int = 0
    zero_char_pages: int = 0
    hits: list[tuple[int, SkipRuleId, bool]] = field(default_factory=list)  # 页, 第一条规则, L0
    all_matched: list[tuple[int, tuple[SkipRuleId, ...]]] = field(default_factory=list)
    blank_loose: int = 0  # 宽松口径：字符<40 且无图无曲线（不要求零数字），用于与预测量对账
    sign_loose: int = 0
    # 文字层字符数 = 0 的页：(页, 绘制分组, 是否 blank_page 命中, 墨迹比 | None)
    zero_text: list[tuple[int, str, bool, float | None]] = field(default_factory=list)
    # blank_page 命中页：(页, 字符数, 绘制分组, 墨迹比 | None)
    blank_hits: list[tuple[int, int, str, float | None]] = field(default_factory=list)


def _rows(cur: Any) -> list[Row]:  # noqa: ANN401
    return [tuple(r) for r in cur.fetchall()]


def _ink(pdf: pdfium.PdfDocument, page: int) -> float | None:
    try:
        bitmap = pdf[page - 1].render(scale=RENDER_SCALE, grayscale=True)
        return ink_ratio(bytes(bitmap.to_numpy().tobytes()))
    except Exception:
        return None


def _scan(item: Item) -> tuple[str, DocResult | None]:
    key, kind, path = item
    try:
        data = Path(path).read_bytes()
        layouts = extract_pages(data)
        l0 = parse_layouts_l0(layouts)
        pdf = pdfium.PdfDocument(data)
    except Exception:
        return key, None
    facts = [page_facts(x) for x in layouts]
    decisions = evaluate_skip_rules(facts, doc_kind=kind)
    res = DocResult(n_pages=len(layouts))
    for d in decisions:
        if d.matched:
            res.all_matched.append((d.page, d.matched))
            res.hits.append((d.page, d.matched[0], l0[d.page - 1].accepted))
    for i, (lay, f, d) in enumerate(zip(layouts, facts, decisions, strict=True)):
        chars = sum(1 for ch in f.text if not ch.isspace())
        group = drawing_group(g.kind for g in lay.graphics)
        if chars < 40 and f.image_count == 0 and f.curve_count == 0:
            res.blank_loose += 1
        last_of_many = kind == "announcement" and i == len(layouts) - 1 and len(layouts) >= 2
        if last_of_many and chars < 200 and _SIGN.search(f.text):
            res.sign_loose += 1
        is_blank = "blank_page" in d.matched
        ink: float | None = None
        if chars == 0 or is_blank:
            ink = _ink(pdf, f.page)
        if chars == 0:
            res.zero_char_pages += 1
            res.zero_text.append((f.page, group, is_blank, ink))
        if is_blank:
            res.blank_hits.append((f.page, chars, group, ink))
    return key, res


def _pages_of(path: Path) -> int:
    try:
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def _pct(n: int, d: int) -> str:
    return f"{n}/{d}" if d else f"{n}/0"


def main() -> None:
    # 可选：--max-doc-id N 把验证集钉在 doc_id <= N（并发入库期间复现登记时的 31 份）
    max_doc_id = (
        int(sys.argv[sys.argv.index("--max-doc-id") + 1]) if "--max-doc-id" in sys.argv else 10**9
    )
    blob_root = load_config().blob_root
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        with as_of_session(conn, datetime.now(UTC)) as s:
            pending = _rows(
                s.execute(
                    "SELECT d.doc_id, d.doc_type, d.title, d.raw_ref, d.source, d.entity_id,"
                    " d.period,"
                    " d.content_hash FROM asof.document d WHERE d.raw_ref LIKE '%.pdf'"
                    " AND NOT EXISTS (SELECT 1 FROM asof.doc_block b WHERE b.doc_id = d.doc_id)"
                )
            )
            done = _rows(
                s.execute(
                    "SELECT doc_id, doc_type, title, raw_ref, content_hash FROM asof.document"
                    f" WHERE parse_engine LIKE 'textin%' AND doc_id <= {max_doc_id}"
                )
            )
            blocks = _rows(
                s.execute(
                    "SELECT b.doc_id, b.page, b.block_id, b.content FROM asof.doc_block b"
                    " JOIN asof.document d USING (doc_id)"
                    f" WHERE d.parse_engine LIKE 'textin%' AND d.doc_id <= {max_doc_id}"
                    " AND b.is_leaf"
                )
            )
            refs = {
                int(r[0])
                for r in _rows(
                    s.execute("SELECT block_id FROM asof.fact_table_ref WHERE block_id IS NOT NULL")
                )
            }
            allrows = _rows(s.execute("SELECT doc_id, entity_id, period, title FROM asof.document"))
        eval_refs = {
            int(b)
            for (arr,) in conn.execute("SELECT gold_block_ids FROM evals.eval_retrieval")
            for b in arr
        }
        eval_refs |= {
            int(r[0])
            for r in conn.execute(
                "SELECT gold_block_id FROM evals.eval_extraction WHERE gold_block_id IS NOT NULL"
            )
        }
        d1 = _rows(
            conn.execute(
                "SELECT doc_id, raw_ref FROM core.document WHERE superseded_at IS NOT NULL"
                " AND raw_ref LIKE '%.pdf'"
            )
        )
    refs |= eval_refs

    def fp(raw: str) -> Path:
        return blob_root / raw

    all_backlog_rows = list(pending)
    periodic = {r[0]: force_whole_document_l3(r[1], r[2], None) for r in all_backlog_rows}
    backlog_rows = [r for r in all_backlog_rows if not periodic[r[0]]]
    backlog: list[Item] = [(f"P:{r[0]}", "announcement", str(fp(r[3]))) for r in backlog_rows]
    research = sorted(glob.glob("docs/rawdata/**/research/*.pdf", recursive=True))
    backlog += [(f"R:{Path(f).name}", "research_report", f) for f in research]
    # 验证集：31 份现行 TextIn 文档，全部扫描；门槛判定只用非定期（定期整份 L3、规则不适用）
    done_periodic = {r[0]: force_whole_document_l3(r[1], r[2], None) for r in done}
    validation: list[Item] = [(f"D:{r[0]}", "announcement", str(fp(r[3]))) for r in done]
    # D4 需要对定期积压的文字层信息，另外扫描（不参与规则统计）
    periodic_items: list[Item] = [
        (f"Q:{r[0]}", "announcement", str(fp(r[3]))) for r in all_backlog_rows if periodic[r[0]]
    ]

    with ProcessPoolExecutor(4) as ex:
        back = dict(ex.map(_scan, backlog, chunksize=4))
        val = dict(ex.map(_scan, validation, chunksize=4))
        per = dict(ex.map(_scan, periodic_items, chunksize=4))

    print(
        f"使用文档：积压公告(非定期) {len(backlog_rows)} 份、研报 {len(research)} 份、"
        f"TextIn 现行文档 {len(done)} 份（其中定期 {sum(done_periodic.values())} 份）"
    )
    print("无法测量：", sorted(k for k, r in {**back, **val, **per}.items() if r is None))

    # ---- 积压候选与节省
    cands: list[BacklogCandidate] = []
    cand_research: list[BacklogCandidate] = []
    pages_total = {"P": 0, "R": 0}
    for key, r in back.items():
        if r is None:
            continue
        pages_total[key[0]] += r.n_pages
        tgt = cands if key[0] == "P" else cand_research
        tgt.extend(BacklogCandidate(rule, key, pg, acc) for pg, rule, acc in r.hits)
    print(f"积压公告非定期 {pages_total['P']} 页；研报 {pages_total['R']} 页")
    for label, group in (("公告积压", cands), ("研报", cand_research)):
        for rule, sv in savings(group, price=DEFAULT_TEXTIN_COST_PER_PAGE_CNY).items():
            print(
                f"[{label}] {rule}: 候选 {sv.candidate_pages} 页，扣 L0 后增量 "
                f"{sv.incremental_pages} 页，{sv.incremental_cny} 元"
            )
    allc = cands + cand_research
    print(
        "[积压总计，去重（一页只计第一条规则）] 候选",
        len(allc),
        "页，增量",
        sum(1 for c in allc if not c.l0_accepted),
        "页",
    )
    print(
        "宽松口径（对账预测量）：积压 blank_loose =",
        sum(r.blank_loose for k, r in back.items() if r and k[0] == "P"),
        "sign_loose =",
        sum(r.sign_loose for k, r in back.items() if r and k[0] == "P"),
        "；TextIn 文档 sign_loose =",
        sum(r.sign_loose for r in val.values() if r),
    )

    # ---- 验证证据（31 份现行 TextIn 文档）
    by_page: dict[tuple[str, int], list[tuple[int, str]]] = {}
    for doc_id, page, block_id, content in blocks:
        by_page.setdefault((f"D:{doc_id}", int(page or 0)), []).append(
            (int(block_id), str(content))
        )
    for label, only_nonperiodic in (("非定期（门槛口径）", True), ("全部 31 份（对照）", False)):
        ev: list[SkipEvidence] = []
        for key, r in val.items():
            if r is None or (only_nonperiodic and done_periodic[int(key[2:])]):
                continue
            for page, rule, _acc in r.hits:
                bl = by_page.get((key, page), [])
                ev.append(
                    SkipEvidence(
                        rule,
                        key,
                        page,
                        sum(non_date_numbers(c) for _, c in bl),
                        sum(1 for b, _ in bl if b in refs),
                    )
                )
        print(f"== 验证判定：{label}")
        for rule in SKIP_RULE_IDS:
            print(judge_rule(rule, ev))
        print(
            "验证跳页清单(doc_id:page)：", sorted(f"{e.doc_key[2:]}:{e.page}({e.rule})" for e in ev)
        )
        print(
            "验证页上 TextIn 块数字：",
            [(f"{e.doc_key[2:]}:{e.page}", e.block_numbers) for e in ev if e.block_numbers],
        )

    # ---- 矢量描边 / 墨迹探测
    print("== 探测：文字层字符数 = 0 的页（按绘制对象分组；是否被 blank_page 命中）")
    groups: Counter[tuple[str, bool]] = Counter()
    ink_gt_nonhit: Counter[str] = Counter()
    for _src, results in (("积压/研报", back), ("TextIn", val), ("定期积压", per)):
        for r in results.values():
            if r is None:
                continue
            for _pg, g, hit, ink in r.zero_text:
                groups[(g, hit)] += 1
                if not hit and ink is not None and ink > INK_RATIO_REVIEW:
                    ink_gt_nonhit[g] += 1
    for (g, hit), n in sorted(groups.items()):
        print(f"  无文字层页 绘制={g} blank命中={hit}: {n}")
    print(
        "  无文字层且未命中 blank、墨迹比>0.5% 的页（说明未命中页确有内容）：", dict(ink_gt_nonhit)
    )
    print("== 探测：blank_page 命中页（全部来源）")
    all_hits = [(k, h) for res in (back, val) for k, r in res.items() if r for h in r.blank_hits]
    print("  命中总数", len(all_hits), "；其中无文字层", sum(1 for _, h in all_hits if h[1] == 0))
    print("  按绘制分组：", dict(Counter(h[2] for _, h in all_hits)))
    inks = [h[3] for _, h in all_hits if h[3] is not None]
    print(
        "  渲染成功",
        len(inks),
        "；墨迹比最大",
        max(inks, default=0.0),
        "；> 0.5% 的页数",
        sum(1 for x in inks if x > INK_RATIO_REVIEW),
    )
    print(
        "  需目检(墨迹比>0.5%) doc:page：",
        sorted(
            f"{k}:{h[0]}(chars={h[1]},{h[2]},ink={h[3]:.4f})"
            for k, h in all_hits
            if h[3] is not None and h[3] > INK_RATIO_REVIEW
        ),
    )
    print("  渲染失败：", sorted(f"{k}:{h[0]}" for k, h in all_hits if h[3] is None))

    # ---- 文档级 D1-D5
    d1_pages = sum(_pages_of(fp(r[1])) for r in d1)
    print(f"D1 已取代：{len(d1)} 份 {d1_pages} 页（_select_pending 已排除）")
    hashes = Counter(r[7] for r in all_backlog_rows)
    parsed_hashes = {r[4] for r in done}
    dup = sum(1 for r in all_backlog_rows if hashes[r[7]] > 1 or r[7] in parsed_hashes)
    print(f"D2 同内容哈希（积压内重复或与已解析同哈希）：{dup} 份")
    full_keys = {(r[1], r[2], r[3]) for r in allrows}
    d3 = []
    for row in all_backlog_rows:
        base = summary_base(row[2])
        if base and any(k[0] == row[5] and k[1] == row[6] and k[2] == base for k in full_keys):
            d3.append(row)
    d3_pages = sum(_pages_of(fp(row[3])) for row in d3)
    print(f"D3 摘要与全文并存（积压内，含定期）：{len(d3)} 份 {d3_pages} 页")
    scans = [
        (k, r)
        for k, r in {**back, **per}.items()
        if r and k[0] in "PQ" and r.n_pages and r.zero_char_pages == r.n_pages
    ]
    print(
        f"D4 整份扫描件：{len(scans)} 份 {sum(r.n_pages for _, r in scans)} 页；",
        sorted(k[2:] for k, _ in scans),
    )
    row_by_id = {f"P:{r[0]}": r for r in all_backlog_rows} | {
        f"Q:{r[0]}": r for r in all_backlog_rows
    }
    d5: dict[bool, list[int]] = {}
    for k, r in {**back, **per}.items():
        if r is None or k[0] not in "PQ":
            continue
        row = row_by_id[k]
        if tier_for_document(row[4], row[1], row[2]) is ParseTier.NONE:
            d5.setdefault(k[0] == "Q", []).append(r.n_pages)
    for is_per, pg in sorted(d5.items()):
        print(
            f"D5 NONE 档 {'定期' if is_per else '非定期'}：{len(pg)} 份 {sum(pg)} 页，"
            f"{DEFAULT_TEXTIN_COST_PER_PAGE_CNY * sum(pg)} 元"
        )


if __name__ == "__main__":
    main()
