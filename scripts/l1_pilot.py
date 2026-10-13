"""L1（自托管 PaddleOCR）表格页试跑：只测量，不改生产模块。

用法（均只读库与 blob；不调任何付费接口；一次一页，严禁并行）：
    set -a && . ./.env && set +a && uv run --no-sync python scripts/l1_pilot.py select
    uv run --no-sync python scripts/l1_pilot.py run [--limit N]
    uv run --no-sync python scripts/l1_pilot.py analyze

select ：从 l0v2/split.tsv 的调参集公告文档里，挑有 TextIn 缓存解析、页型为表格/财务报表且
         TextIn 在该页有表格的页，按种子 20261020 分层抽 40 页，写
         data/experiments/l1-pilot/sample.tsv，并把参考网格缓存到 ref/（均被 git 忽略）。
run    ：逐页调 paddle 网关，记录耗时、`docker stats` 峰值内存、OOMKilled / RestartCount，
         逐页追加 results.jsonl（可断点续跑）。连续 3 页被杀或超过 180 s 即停。
analyze：离线算全部指标（不需要库或容器）。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

OUT = Path("data/experiments/l1-pilot")
SPLIT = Path("docs/10.12-plan/l0v2/split.tsv")
SEED = 20261020
SAMPLE_SIZE = 40
CONTAINER = "ragdemo-parse-paddle"
BASE_URL = "http://127.0.0.1:8091"
TIMEOUT_S = 180.0
STOP_AFTER_CONSECUTIVE = 3

KINDS = (
    "balance_sheet",
    "income_statement",
    "cash_flow",
    "key_figures",
    "multi_table",
    "multi_row_header",
    "small_table",
    "other_table",
)

# 路由计划成本公式的输入（盘点 §6 普查，路由范围）
TEXTIN_CNY = 0.1356
CENSUS = {
    # 组: (页数, 无文字层, 财务报表页)
    "announcement": (4348, 793, 40),
    "research": (596, 7, 90),
}


# ------------------------------------------------------------------ 纯函数（有单测）


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def page_kind_label(text: str, n_tables: int, n_rows: int, header_rows: int) -> str:
    """按页面文字层与参考网格给表格页贴分层标签（先到先得）。"""
    flat = re.sub(r"\s+", "", text)
    if "现金流量表" in flat:
        return "cash_flow"
    if "利润表" in flat or "损益表" in flat:
        return "income_statement"
    if "资产负债表" in flat:
        return "balance_sheet"
    if any(k in flat for k in ("主要财务数据", "主要会计数据", "主要财务指标", "财务数据")):
        return "key_figures"
    if n_tables >= 2:
        return "multi_table"
    if header_rows >= 2:
        return "multi_row_header"
    if n_rows <= 6:
        return "small_table"
    return "other_table"


def stratified_pick(
    candidates: Sequence[tuple[str, int, str]], n: int, seed: int
) -> list[tuple[str, int, str]]:
    """candidates: (doc_key, page, kind)。按 kind 轮转抽取（每层先随机洗牌），直到 n 或耗尽。
    返回按 (doc_key, page) 排序的结果，同一输入同一种子结果相同。"""
    rng = random.Random(seed)
    buckets: dict[str, list[tuple[str, int, str]]] = {k: [] for k in KINDS}
    for c in sorted(candidates):
        buckets.setdefault(c[2], []).append(c)
    for k in sorted(buckets):
        rng.shuffle(buckets[k])
    picked: list[tuple[str, int, str]] = []
    while len(picked) < n and any(buckets.values()):
        for k in sorted(buckets):
            if buckets[k] and len(picked) < n:
                picked.append(buckets[k].pop())
    return sorted(picked)


_TABLE_RE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)


def table_slices(md: str) -> list[str]:
    return _TABLE_RE.findall(md)


def norm_cell(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def grid_shape(cells: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    return (
        max(c["row"] + c["row_span"] for c in cells),
        max(c["col"] + c["col_span"] for c in cells),
    )


def cell_texts(cells: Sequence[Mapping[str, Any]], *, nospace: bool = False) -> Counter[str]:
    if nospace:
        return Counter(re.sub(r"\s+", "", norm_cell(str(c["text"]))) for c in cells)
    return Counter(norm_cell(str(c["text"])) for c in cells)


def table_number_counter(cells: Sequence[Mapping[str, Any]]) -> Counter[str]:
    from ragdemo.parse.local.number_closure import numbers_in

    total: Counter[str] = Counter()
    for c in cells:
        total.update(numbers_in(norm_cell(str(c["text"]))))
    return total


def row_tuples(cells: Sequence[Mapping[str, Any]]) -> Counter[tuple[str, ...]]:
    rows: dict[int, list[tuple[int, str]]] = {}
    for c in cells:
        rows.setdefault(int(c["row"]), []).append((int(c["col"]), norm_cell(str(c["text"]))))
    return Counter(tuple(t for _, t in sorted(v)) for v in rows.values())


def micro_counts(ref: Counter[str], eng: Counter[str]) -> tuple[int, int, int]:
    """(交集数, 参考总数, 引擎总数)，多重集合口径。"""
    inter = sum((ref & eng).values())
    return inter, sum(ref.values()), sum(eng.values())


def compare_pair(
    ref: Sequence[Mapping[str, Any]], eng: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    shape_eq = grid_shape(ref) == grid_shape(eng)
    text_eq = cell_texts(ref) == cell_texts(eng)
    num_eq = table_number_counter(ref) == table_number_counter(eng)
    text_eq_ns = cell_texts(ref, nospace=True) == cell_texts(eng, nospace=True)
    rr, er = row_tuples(ref), row_tuples(eng)
    row_hit = sum((rr & er).values())
    return {
        "shape_eq": shape_eq,
        "text_eq": text_eq,
        "num_eq": num_eq,
        "exact": shape_eq and text_eq and num_eq,
        "text_eq_nospace": text_eq_ns,
        "exact_nospace": shape_eq and text_eq_ns and num_eq,
        "ref_rows": sum(rr.values()),
        "row_hits": row_hit,
        "ref_shape": list(grid_shape(ref)),
        "eng_shape": list(grid_shape(eng)),
    }


def compare_tables(
    ref: Sequence[Sequence[Mapping[str, Any]]], eng: Sequence[Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    """逐参考表比较：按顺序配对（引擎表数不同则无法配对的参考表算不等）。
    另报"任意配对"：参考表与某个引擎表完全相等（顺序/拆并之外的上界）。"""
    pairs: list[dict[str, Any]] = []
    for i, r in enumerate(ref):
        if i < len(eng):
            pr = compare_pair(r, eng[i])
        else:
            pr = {
                "shape_eq": False,
                "text_eq": False,
                "num_eq": False,
                "exact": False,
                "text_eq_nospace": False,
                "exact_nospace": False,
                "ref_rows": sum(row_tuples(r).values()),
                "row_hits": 0,
                "ref_shape": list(grid_shape(r)),
                "eng_shape": None,
            }
        pr["any_order_exact"] = any(compare_pair(r, e)["exact"] for e in eng)
        pairs.append(pr)
    return {"count_equal": len(ref) == len(eng), "pairs": pairs}


def classify_failure(
    *,
    error: str | None,
    markdown: str,
    verdict_ok: bool | None,
    reasons: Sequence[str],
    markup: Sequence[str],
    grids_ok: bool,
) -> list[str]:
    """把一页的失败拆成标签（可多个）；通过则返回空列表。"""
    if error:
        return [
            "engine_timeout" if "不可达" in error or "timeout" in error.lower() else "engine_error"
        ]
    out: list[str] = []
    if not markdown.strip():
        return ["empty_output"]
    if markup:
        out.append("markup_violation")
    text = "；".join(reasons)
    if "凭空数字" in text:
        out.append("invented_digits")
    if "丢失数字" in text:
        out.append("missing_digits")
    if "关键字新增" in text or "关键字丢失" in text or "覆盖率" in text:
        out.append("text_char_diff")
    if "顺序" in text:
        out.append("order_mismatch")
    if "表" in text and ("行" in text or "结构" in text or "列" in text):
        out.append("table_structure")
    if verdict_ok is False and not out:
        out.append("other_verify")
    if verdict_ok and not grids_ok:
        out.append("grid_conversion_failed")
    return out


def pct(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def blended_cost(
    *,
    scan_share: float,
    statement_share: float,
    l1_pass: float,
    c_textin: float = TEXTIN_CNY,
    c_local: float = 0.0,
    l0_pass: float = 0.0,
    c_l0: float = 0.0,
) -> float:
    """路由计划 T7 `blended_cost_per_page` 的原样实现（l1/l2/l0 开关取：L0 关、L1 开、L2 关）。"""
    after = c_local + (1 - l1_pass) * c_textin
    text_non_stmt = (1 - scan_share) * (1 - statement_share)
    return (
        (1 - scan_share) * statement_share * c_textin
        + text_non_stmt * (c_l0 + (1 - l0_pass) * after)
        + scan_share * c_textin
    )


def census_shares(groups: Sequence[str]) -> tuple[float, float]:
    pages = sum(CENSUS[g][0] for g in groups)
    scan = sum(CENSUS[g][1] for g in groups)
    stmt = sum(CENSUS[g][2] for g in groups)
    return scan / pages, stmt / (pages - scan)


def parse_mem_gib(text: str) -> float | None:
    """'12.3GiB / 13GiB' 或 '512MiB / 13GiB' -> 已用 GiB。"""
    m = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)", text)
    if not m:
        return None
    v = float(m.group(1))
    scale = {"B": 1 / 2**30, "KiB": 1 / 2**20, "MiB": 1 / 2**10, "GiB": 1.0, "TiB": 1024.0}
    return v * scale.get(m.group(2), 0.0) if m.group(2) in scale else None


# ------------------------------------------------------------------ 选样


def _load_split_docs() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, line in enumerate(SPLIT.read_text(encoding="utf-8").splitlines()):
        parts = line.split("\t")
        if i == 0 or len(parts) < 4:
            continue
        if parts[0] == "docs" and parts[3] == "tune":
            out.append((int(parts[1]), parts[2]))
    return out


def cmd_select() -> None:
    import psycopg

    from ragdemo.config import load_config
    from ragdemo.parse.local.layout import extract_pages
    from ragdemo.parse.textin_audit import page_offset
    from ragdemo.parse.tiered.census import classify_pages
    from ragdemo.tables.textin_cells import grid_from_textin_item
    from ragdemo_core.blob import LocalBlobStore

    blob = LocalBlobStore(load_config().blob_root)
    docs = _load_split_docs()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ref").mkdir(exist_ok=True)
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT doc_id, doc_type, parse_engine, parse_json_ref, raw_ref FROM core.document"
            " WHERE doc_id = ANY(%s) AND parse_json_ref IS NOT NULL ORDER BY doc_id",
            ([d for d, _ in docs],),
        ).fetchall()
    stratum = dict(docs)
    cands: list[tuple[str, int, str]] = []
    info: dict[str, dict[str, Any]] = {}
    for doc_id, doc_type, engine, json_ref, raw_ref in rows:
        payload = json.loads(blob.get(str(json_ref)))
        detail = (payload.get("result") or {}).get("detail") or []
        off = page_offset(detail)
        layouts = extract_pages(blob.get(str(raw_ref)))
        kinds = classify_pages(layouts)
        by_page: dict[int, list[dict[str, Any]]] = {}
        for item in detail:
            if item.get("type") != "table" or int(item.get("content", 0) or 0) == 1:
                continue
            raw_cells = item.get("cells") or []
            if not raw_cells:
                continue
            split = len(item.get("split_section_page_ids") or []) > 1
            g = grid_from_textin_item(
                item, page_offset=off, page_dims={}, engine=str(engine), provider_ref=None
            )
            if g is None:
                continue
            # 跨页表：按单元格自带页码切出落在该页的部分（缺页码的格子 -> 整表不用，不猜）。
            if split and any(c.page is None for c in g.cells):
                continue
            pages_of = sorted({c.page for c in g.cells if c.page is not None})
            for pg in pages_of:
                cs = [c for c in g.cells if c.page == pg]
                r0 = min(c.row for c in cs)
                rmax = max(c.row for c in cs)
                mapped = [
                    {
                        "row": c.row - r0,
                        "col": c.col,
                        "row_span": min(c.row_span, rmax - c.row + 1),
                        "col_span": c.col_span,
                        "text": c.text,
                    }
                    for c in cs
                ]
                by_page.setdefault(pg, []).append(
                    {"cells": mapped, "split": split, "header_rows": 0 if split else g.header_rows}
                )
        for pg, tables in sorted(by_page.items()):
            if pg > len(layouts) or kinds[pg - 1] not in ("table", "financial_statement"):
                continue
            label = page_kind_label(
                layouts[pg - 1].text,
                len(tables),
                max(grid_shape(t["cells"])[0] for t in tables),
                max(t["header_rows"] for t in tables),
            )
            key = f"{doc_id}:{pg}"
            cands.append((str(doc_id), pg, label))
            info[key] = {
                "doc_id": int(doc_id),
                "page": pg,
                "census_kind": kinds[pg - 1],
                "doc_type": str(doc_type),
                "stratum": stratum[int(doc_id)],
                "raw_ref": str(raw_ref),
                "ref": tables,
            }
    print(f"候选页（全部调参集文档）：{len(cands)}；按标签：{dict(Counter(c[2] for c in cands))}")
    ann = [c for c in cands if info[f"{c[0]}:{c[1]}"]["stratum"] == "announcement"]
    print(
        f"其中 announcement 层文档的候选页：{len(ann)}；按标签：{dict(Counter(c[2] for c in ann))}"
    )
    pool = ann
    if len(ann) < SAMPLE_SIZE:
        print(
            f"announcement 层不足 {SAMPLE_SIZE} 页，补入同一调参集的 annual_report/quarterly 文档页"
        )
        pool = cands
    picked = stratified_pick(pool, SAMPLE_SIZE, SEED)
    lines = ["sample_id\tdoc_id\tpage\tlabel\tcensus_kind\tdoc_stratum\tdoc_type\tn_ref_tables"]
    for i, (d, pg, label) in enumerate(picked, 1):
        rec = info[f"{d}:{pg}"]
        lines.append(
            f"{i}\t{d}\t{pg}\t{label}\t{rec['census_kind']}\t{rec['stratum']}"
            f"\t{rec['doc_type']}\t{len(rec['ref'])}"
        )
        (OUT / "ref" / f"{i}.json").write_text(
            json.dumps({**rec, "label": label}, ensure_ascii=False), encoding="utf-8"
        )
    (OUT / "sample.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写 {OUT / 'sample.tsv'}：{len(picked)} 页；{dict(Counter(p[2] for p in picked))}")


# ------------------------------------------------------------------ 运行


def _docker(*args: str, timeout: float = 30) -> str:
    r = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    return r.stdout.strip()


def container_state() -> dict[str, Any]:
    raw = _docker(
        "inspect",
        "-f",
        "{{.State.Status}}|{{.State.OOMKilled}}|{{.RestartCount}}|{{.State.StartedAt}}",
        CONTAINER,
    )
    parts = raw.split("|")
    if len(parts) != 4:
        return {"status": "unknown", "oom": None, "restarts": None, "started": raw}
    return {
        "status": parts[0],
        "oom": parts[1] == "true",
        "restarts": int(parts[2]),
        "started": parts[3],
    }


class MemSampler(threading.Thread):
    """连续轮询 `docker stats --no-stream`（每次约 2-4 秒，首选的流式模式在管道下无输出）。只读。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[float] = []
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                raw = _docker(
                    "stats", "--no-stream", "--format", "{{.MemUsage}}", CONTAINER, timeout=30
                )
                v = parse_mem_gib(raw)
                if v is not None:
                    self.samples.append(v)
            except Exception:
                self._halt.wait(1.0)

    def finish(self) -> float | None:
        self._halt.set()
        self.join(timeout=35)
        return max(self.samples) if self.samples else None


def wait_health(limit_s: float = 300) -> bool:
    import httpx

    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            r = httpx.get(f"{BASE_URL}/v1/health", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def _read_sample() -> list[dict[str, str]]:
    lines = (OUT / "sample.tsv").read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"), strict=True)) for ln in lines[1:]]


def cmd_run(limit: int | None) -> None:
    from ragdemo.config import load_config
    from ragdemo.parse.tiered.engines import EngineError
    from ragdemo.parse.tiered.http_engine import engine_from_url
    from ragdemo.parse.tiered.pdfpages import single_page_pdf
    from ragdemo_core.blob import LocalBlobStore

    blob = LocalBlobStore(load_config().blob_root)
    engine = engine_from_url("paddle", BASE_URL)
    results = OUT / "results.jsonl"
    (OUT / "md").mkdir(exist_ok=True)
    done = set()
    if results.exists():
        done = {
            json.loads(ln)["sample_id"]
            for ln in results.read_text(encoding="utf-8").splitlines()
            if ln
        }
    consecutive = 0
    ran = 0
    for row in _read_sample():
        sid = int(row["sample_id"])
        if sid in done:
            continue
        if limit is not None and ran >= limit:
            break
        ref = json.loads((OUT / "ref" / f"{sid}.json").read_text(encoding="utf-8"))
        pdf = blob.get(ref["raw_ref"])
        page_pdf = single_page_pdf(pdf, int(row["page"]))
        before = container_state()
        sampler = MemSampler()
        sampler.start()
        t0 = time.perf_counter()
        error: str | None = None
        markdown = ""
        try:
            out = engine.parse_page(page_pdf, page_no=int(row["page"]))
            markdown = out.markdown
        except EngineError as exc:
            error = f"{type(exc).__name__}: {exc}"
        secs = time.perf_counter() - t0
        peak = sampler.finish()
        after = container_state()
        killed = bool(
            after["restarts"] is not None
            and before["restarts"] is not None
            and (after["restarts"] > before["restarts"] or after["started"] != before["started"])
        ) or bool(after["oom"])
        restarted_by_me = False
        drain_s: float | None = None
        abort = False
        if (
            error is not None
            and secs >= TIMEOUT_S - 1
            and after["status"] == "running"
            and not killed
        ):
            # 客户端超时后引擎仍在算这一页（单线程薄壳无法取消）：等它空闲，记真实总耗时
            td = time.perf_counter()
            idle = wait_health(limit_s=2700)
            drain_s = round(time.perf_counter() - td, 1)
            if not idle:
                abort = True
        if after["status"] != "running" or killed:
            # 仅当被 OOM 杀才重启一次；restart 策略已拉起则只等健康
            if after["status"] != "running":
                _docker("restart", CONTAINER, timeout=120)
                restarted_by_me = True
            wait_health()
        rec = {
            "sample_id": sid,
            "doc_id": int(row["doc_id"]),
            "page": int(row["page"]),
            "label": row["label"],
            "seconds": round(secs, 2),
            "engine_ms": None,
            "peak_gib": peak,
            "error": error,
            "oom_or_restart": killed,
            "oom_flag": after["oom"],
            "restart_count": after["restarts"],
            "restarted_by_runner": restarted_by_me,
            "timeout_gt_180": secs > TIMEOUT_S,
            "drain_seconds": drain_s,
            "md_chars": len(markdown),
        }
        (OUT / "md" / f"{sid}.md").write_text(markdown, encoding="utf-8")
        with results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(
            f"[{sid}] doc={rec['doc_id']} p{rec['page']} {row['label']} {secs:.1f}s peak={peak}"
            f" err={error} killed={killed}",
            flush=True,
        )
        ran += 1
        if abort:
            print("引擎 45 分钟仍未空闲，停止。", flush=True)
            sys.exit(4)
        bad = killed or secs > TIMEOUT_S or error is not None
        consecutive = consecutive + 1 if bad and (killed or secs > TIMEOUT_S) else 0
        if consecutive >= STOP_AFTER_CONSECUTIVE:
            print("连续 3 页被杀/超时 180s，停止。", flush=True)
            sys.exit(3)


# ------------------------------------------------------------------ 分析


def cmd_analyze() -> None:
    from ragdemo.config import load_config
    from ragdemo.parse.local.compare import plain_text_from_markdown
    from ragdemo.parse.local.layout import extract_pages
    from ragdemo.parse.local.number_closure import numbers_in
    from ragdemo.parse.tiered.pdfpages import single_page_pdf
    from ragdemo.parse.tiered.verify import markup_violations, verify_against_text_layer
    from ragdemo.tables.html_grid import grid_from_html
    from ragdemo_core.blob import LocalBlobStore

    blob = LocalBlobStore(load_config().blob_root)
    results = [
        json.loads(ln)
        for ln in (OUT / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if ln
    ]
    pages_out: list[dict[str, Any]] = []
    fail_reasons: Counter[str] = Counter()
    d_inter = d_ref = d_eng = 0
    t_exact = t_total = t_any = t_shape = t_text = t_num = 0
    rows_hit = rows_ref = 0
    subset_pages = 0
    eng_tables_total = 0
    count_equal_pages = 0
    shape_only_fail = 0
    t_exact_ns = t_text_ns = 0
    ld_inter = ld_layer = ld_eng = 0
    relaxed_pass = digits_ok = 0
    for rec in results:
        sid = rec["sample_id"]
        ref = json.loads((OUT / "ref" / f"{sid}.json").read_text(encoding="utf-8"))
        md = (OUT / "md" / f"{sid}.md").read_text(encoding="utf-8")
        page_pdf = single_page_pdf(blob.get(ref["raw_ref"]), rec["page"])
        layer = extract_pages(page_pdf)[0].text
        verdict = verify_against_text_layer(md, layer) if not rec["error"] else None
        markup = markup_violations(md) if md else []
        slices = table_slices(md)
        grids = [grid_from_html(s, page=rec["page"], engine="paddle") for s in slices]
        grids_ok = bool(slices) and all(g is not None for g in grids)
        eng_cells = [g.as_cell_mappings() for g in grids if g is not None]
        ref_cells = [t["cells"] for t in ref["ref"]]
        cmp = compare_tables(ref_cells, eng_cells)
        eng_nums: Counter[str] = Counter()
        for cs in eng_cells:
            eng_nums.update(table_number_counter(cs))
        ref_nums: Counter[str] = Counter()
        for cs in ref_cells:
            ref_nums.update(table_number_counter(cs))
        i, r, e = micro_counts(ref_nums, eng_nums)
        d_inter += i
        d_ref += r
        d_eng += e
        layer_nums = numbers_in(layer)
        page_nums = numbers_in(plain_text_from_markdown(md)) if md else Counter()
        li, lr, le = micro_counts(layer_nums, page_nums)
        ld_inter += li
        ld_layer += lr
        ld_eng += le
        subset = not (eng_nums - layer_nums)
        subset_pages += int(subset)
        l1_pass = bool(verdict and verdict.ok and grids_ok)
        tags = classify_failure(
            error=rec["error"],
            markdown=md,
            verdict_ok=None if verdict is None else verdict.ok,
            reasons=verdict.reasons if verdict else (),
            markup=markup,
            grids_ok=grids_ok,
        )
        if not l1_pass:
            fail_reasons.update(tags or ["unknown"])
        blocking = set(tags) - {"order_mismatch", "table_structure"}
        relaxed_pass += int(not rec["error"] and bool(slices) and not blocking and grids_ok)
        digits_ok += int(
            not rec["error"] and "invented_digits" not in tags and "missing_digits" not in tags
        )
        eng_tables_total += len(eng_cells)
        count_equal_pages += int(cmp["count_equal"])
        for p in cmp["pairs"]:
            t_total += 1
            t_exact += int(p["exact"])
            t_exact_ns += int(p["exact_nospace"])
            t_text_ns += int(p["text_eq_nospace"])
            t_any += int(p["any_order_exact"])
            t_shape += int(p["shape_eq"])
            t_text += int(p["text_eq"])
            t_num += int(p["num_eq"])
            rows_hit += p["row_hits"]
            rows_ref += p["ref_rows"]
            if (not p["shape_eq"]) and p["text_eq"] and p["num_eq"]:
                shape_only_fail += 1
        pages_out.append(
            {
                **rec,
                "l1_pass": l1_pass,
                "verdict_ok": None if verdict is None else verdict.ok,
                "reasons": list(verdict.reasons) if verdict else [],
                "markup": markup,
                "n_eng_tables": len(slices),
                "n_ref_tables": len(ref_cells),
                "grids_ok": grids_ok,
                "tags": tags,
                "count_equal": cmp["count_equal"],
                "pairs": cmp["pairs"],
                "eng_nums_subset_of_layer": subset,
                "page_ok_and_exact": l1_pass and all(p["exact"] for p in cmp["pairs"]),
            }
        )
    n = len(pages_out)
    k_pass = sum(p["l1_pass"] for p in pages_out)
    secs = [p["seconds"] for p in pages_out if not p["error"]]
    peaks = [p["peak_gib"] for p in pages_out if p["peak_gib"] is not None]
    oom = sum(1 for p in pages_out if p["oom_or_restart"] or p["oom_flag"])
    by_label: dict[str, dict[str, int]] = {}
    for p in pages_out:
        b = by_label.setdefault(p["label"], {"n": 0, "pass": 0})
        b["n"] += 1
        b["pass"] += int(p["l1_pass"])
    summary: dict[str, Any] = {
        "pages": n,
        "l1_pass": [k_pass, n, k_pass / n if n else None, wilson(k_pass, n)],
        "verify_ok": sum(1 for p in pages_out if p["verdict_ok"]),
        "grids_ok_pages": sum(p["grids_ok"] for p in pages_out),
        "tables_ref": t_total,
        "grid_exact": [
            t_exact,
            t_total,
            t_exact / t_total if t_total else None,
            wilson(t_exact, t_total),
        ],
        "grid_exact_any_order": [t_any, t_total],
        "grid_exact_nospace": [t_exact_ns, t_total, wilson(t_exact_ns, t_total)],
        "text_eq_nospace": t_text_ns,
        "pages_without_digit_diffs": [digits_ok, n],
        "relaxed_pass_no_order_no_structure": [relaxed_pass, n, wilson(relaxed_pass, n)],
        "layer_digit_recall": ld_inter / ld_layer if ld_layer else None,
        "layer_digit_precision": ld_inter / ld_eng if ld_eng else None,
        "layer_digit_counts": [ld_inter, ld_layer, ld_eng],
        "shape_eq": t_shape,
        "text_eq": t_text,
        "num_eq": t_num,
        "shape_only_fail": shape_only_fail,
        "count_equal_pages": count_equal_pages,
        "row_recall": [rows_hit, rows_ref, rows_hit / rows_ref if rows_ref else None],
        "digit_recall": d_inter / d_ref if d_ref else None,
        "digit_precision": d_inter / d_eng if d_eng else None,
        "digit_counts": [d_inter, d_ref, d_eng],
        "eng_nums_subset_of_layer_pages": [subset_pages, n],
        "seconds_mean": statistics.fmean(secs) if secs else None,
        "seconds_median": statistics.median(secs) if secs else None,
        "seconds_p95": pct(secs, 0.95),
        "seconds_max": max(secs) if secs else None,
        "peak_gib_max": max(peaks) if peaks else None,
        "peak_gib_median": statistics.median(peaks) if peaks else None,
        "oom_pages": oom,
        "fail_reasons": dict(fail_reasons),
        "by_label": by_label,
    }
    # 吞吐与成本
    if summary["seconds_mean"]:
        pph = 3600 / summary["seconds_mean"]
        summary["pages_per_hour"] = pph
    lo, hi = wilson(k_pass, n)
    cost: dict[str, Any] = {}
    for name, groups in (
        ("announcement", ["announcement"]),
        ("research", ["research"]),
        ("routing_scope", ["announcement", "research"]),
    ):
        scan, stmt = census_shares(groups)
        cost[name] = {
            "scan_share": scan,
            "statement_share": stmt,
            "at_measured": blended_cost(scan_share=scan, statement_share=stmt, l1_pass=k_pass / n),
            "at_ci_low": blended_cost(scan_share=scan, statement_share=stmt, l1_pass=lo),
            "at_ci_high": blended_cost(scan_share=scan, statement_share=stmt, l1_pass=hi),
            "at_pass_1": blended_cost(scan_share=scan, statement_share=stmt, l1_pass=1.0),
        }
    summary["cost"] = cost
    (OUT / "analysis.json").write_text(
        json.dumps({"summary": summary, "pages": pages_out}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "select":
        cmd_select()
    elif cmd == "run":
        limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
        cmd_run(limit)
    elif cmd == "analyze":
        cmd_analyze()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
