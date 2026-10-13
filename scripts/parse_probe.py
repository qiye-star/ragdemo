"""解析分级路由计划 T1 前置探测。只读：不调任何付费接口，不写库。

用法：
    uv run --no-sync python scripts/parse_probe.py hardware
    uv run --no-sync python scripts/parse_probe.py scan docs/rawdata/688041-hygon
    set -a && . ./.env && set +a && uv run --no-sync python scripts/parse_probe.py scan-db
    set -a && . ./.env && set +a && uv run --no-sync python scripts/parse_probe.py census
census：页型普查（库内全部现行公告 PDF + docs/rawdata/**/research/*.pdf），输出
data/experiments/parse-census-<日期>.tsv；只读，不调付费接口，TSV 只含计数与文档键。
输出：data/experiments/parse-probe-<模式>-<日期>.tsv（data/ 已被 git 忽略）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from io import BytesIO
from pathlib import Path

import psycopg

from ragdemo.config import load_config
from ragdemo.parse.local.layout import extract_pages
from ragdemo.parse.local.pipeline import needs_handback
from ragdemo.parse.tiered.census import (
    GROUPS,
    PAGE_KINDS,
    ROUTING_GROUPS,
    Group,
    PageKind,
    classify_pages,
    force_whole_document_l3,
    is_periodic_report,
    summarize,
)
from ragdemo.parse.tiered.probe import (
    SCAN_DOC_RATIO_MIN,
    SCAN_PAGE_RATIO_MIN,
    l2_decision,
    summarize_scan,
)
from ragdemo_core.blob import LocalBlobStore

OUT_DIR = Path("data/experiments")


def _run(cmd: list[str]) -> str:
    if shutil.which(cmd[0]) is None:
        return f"<{cmd[0]} 不可用>"
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    return (p.stdout or p.stderr).strip()


def hardware() -> None:
    print(f"cpu_count={os.cpu_count()}")
    print(
        "docker:",
        _run(["docker", "info", "--format", "{{.ServerVersion}} cpus={{.NCPU}} mem={{.MemTotal}}"]),
    )
    print(
        "gpu:",
        _run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]
        ),
    )


def _scan(named: Iterable[tuple[str, str, bytes]], mode: str) -> None:
    """named：(文档名, 分组键, PDF 字节)。分组键是 doc_type（scan-db）或 "rawdata"（scan）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-probe-{mode}-{date.today()}.tsv"
    groups: dict[str, dict[str, list[str | None]]] = {}
    with out.open("w", encoding="utf-8") as fh:
        fh.write("doc\tgroup\tpages\tno_text\tcid\n")
        for name, group, data in named:
            try:
                reasons = [needs_handback(p.text) for p in extract_pages(data)]
            except Exception as exc:  # 坏 PDF（pdfminer 异常类型不统一）：记 ERR 行，不中断探测
                fh.write(f"{name}\t{group}\tERR:{type(exc).__name__}\t\t\n")
                continue
            groups.setdefault(group, {})[name] = reasons
            bad = [r for r in reasons if r is not None]
            cid = sum(1 for r in bad if "cid" in r)
            fh.write(f"{name}\t{group}\t{len(reasons)}\t{len(bad)}\t{cid}\n")
    print(out)
    print(f"门槛：页占比 >= {SCAN_PAGE_RATIO_MIN} 或文档占比 >= {SCAN_DOC_RATIO_MIN} 则建 L2")
    scope: dict[str, list[str | None]] = {}
    for group, docs in sorted(groups.items()):
        s = summarize_scan(docs)
        print(
            f"[{group}] docs={s.docs} pages={s.pages} no_text={s.no_text_pages} cid={s.cid_pages}"
            f" page_ratio={s.page_ratio:.4f} doc_ratio={s.doc_ratio:.4f}"
        )
        if group in ("rawdata", "research_report", "announcement"):
            scope.update({f"{group}/{k}": v for k, v in docs.items()})
    total = summarize_scan(scope)
    print(
        f"[分级路由范围合计] page_ratio={total.page_ratio:.4f} doc_ratio={total.doc_ratio:.4f}"
        f" → {l2_decision(total)}"
    )


def scan_dir(directory: Path) -> None:
    _scan(((str(p), "rawdata", p.read_bytes()) for p in sorted(directory.rglob("*.pdf"))), "scan")


def scan_db() -> None:
    blob = LocalBlobStore(load_config().blob_root)
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        rows = conn.execute(
            "SELECT doc_id, doc_type, raw_ref FROM core.document"
            " WHERE superseded_at IS NULL AND raw_ref IS NOT NULL ORDER BY doc_id"
        ).fetchall()
    _scan(((str(d), str(t), blob.get(str(r))) for d, t, r in rows), "scan-db")


MAX_CENSUS_PAGES = 250  # 超过的文档跳过并计数（盘点口径：3 份年报 > 200 页）
_CensusJob = tuple[str, Group, str, int | None, bytes]  # 键、分组、标题、库内页数、PDF 字节
_CensusOut = tuple[
    str, str, int, list[PageKind] | None
]  # 键、状态(ok|skip|err:<异常类>)、页数、页型


def _census_one(job: _CensusJob) -> _CensusOut:
    key, _group, _title, _pc, data = job
    try:
        from pypdf import PdfReader

        n = len(PdfReader(BytesIO(data)).pages)
        if n > MAX_CENSUS_PAGES:
            return key, "skip", n, None
        kinds = classify_pages(extract_pages(data))
        return key, "ok", len(kinds), kinds
    except Exception as exc:  # 坏 PDF（异常类型不统一）：记 err 行，不中断
        return key, f"err:{type(exc).__name__}", 0, None


def _census_jobs() -> list[_CensusJob]:
    jobs: list[_CensusJob] = []
    blob = LocalBlobStore(load_config().blob_root)
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        rows = conn.execute(
            "SELECT doc_id, doc_type, title, page_count, raw_ref FROM core.document"
            " WHERE superseded_at IS NULL AND raw_ref IS NOT NULL ORDER BY doc_id"
        ).fetchall()
    for doc_id, doc_type, title, page_count, raw_ref in rows:
        data = blob.get(str(raw_ref))
        if not data.startswith(b"%PDF"):  # edgar 8-K 是 .htm
            continue
        periodic = force_whole_document_l3(str(doc_type), title, None) or (
            is_periodic_report(title, None, page_count) is not None
        )
        group: Group = "periodic" if periodic else "announcement"
        jobs.append((f"db:{doc_id}", group, str(title or ""), page_count, data))
    for p in sorted(Path("docs/rawdata").rglob("research/*.pdf")):
        jobs.append((f"research:{p.name}", "research", "", None, p.read_bytes()))
    return jobs


def census() -> None:
    jobs = _census_jobs()
    meta = {j[0]: j for j in jobs}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-census-{date.today()}.tsv"
    results: list[_CensusOut] = []
    workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, r in enumerate(pool.map(_census_one, jobs, chunksize=1), start=1):
            results.append(r)
            if i % 25 == 0 or i == len(jobs):
                print(f"[census] {i}/{len(jobs)}", file=sys.stderr, flush=True)
    results.sort(key=lambda r: r[0])
    kinds_by_doc: dict[str, tuple[Group, list[PageKind]]] = {}
    skipped: list[_CensusOut] = []
    errors: Counter[str] = Counter()
    with out.open("w", encoding="utf-8") as fh:
        fh.write("doc\tgroup\tstatus\tpages\t" + "\t".join(PAGE_KINDS) + "\n")
        for key, status, pages, kinds in results:
            group = meta[key][1]
            counts = Counter(kinds or [])
            fh.write(
                f"{key}\t{group}\t{status}\t{pages}\t"
                + "\t".join(str(counts[k]) for k in PAGE_KINDS)
                + "\n"
            )
            if kinds is not None:
                kinds_by_doc[key] = (group, kinds)
            elif status == "skip":
                skipped.append((key, status, pages, None))
            else:
                errors[group] += 1
    print(out)
    c = summarize(kinds_by_doc)
    print(f"口径：普查代理（无装饰剥离、无图表检测）；超过 {MAX_CENSUS_PAGES} 页跳过")
    print("group\tdocs\tpages\t" + "\t".join(PAGE_KINDS))
    for g in (*GROUPS, "routing"):
        gs: tuple[Group, ...] = ROUTING_GROUPS if g == "routing" else (g,)  # type: ignore[assignment]
        cells = [f"{c.kind_count(k, gs)} ({c.share(k, gs):.1%})" for k in PAGE_KINDS]
        ndocs = sum(c.docs.get(x, 0) for x in gs)
        print(f"{g}\t{ndocs}\t{c.page_count(gs)}\t" + "\t".join(cells))
    print(f"scan_share(路由范围)={c.scan_share():.4f}")
    print(f"statement_share(路由范围)={c.statement_share():.4f}")
    print(
        f"跳过（> {MAX_CENSUS_PAGES} 页）{len(skipped)} 份：{[(k, p) for k, _s, p, _k in skipped]}"
    )
    print(f"读取失败：{dict(errors)}")
    print("定期报告（库内标题识别）：doc_id\t类型\t页数\t标题(<=40字)")
    for key, (group, kinds) in sorted(kinds_by_doc.items()):
        if group == "periodic":
            _, _g, title, _pc, _d = meta[key]
            print(
                f"{key}\t{is_periodic_report(title, None, len(kinds))}\t{len(kinds)}\t{title[:40]}"
            )
    for key, _status, pages, _k in skipped:
        if meta[key][1] == "periodic":
            print(f"{key}\t(跳过)\t{pages}\t{meta[key][2][:40]}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "hardware"
    if mode == "hardware":
        hardware()
    elif mode == "scan":
        scan_dir(Path(sys.argv[2]))
    elif mode == "scan-db":
        scan_db()
    elif mode == "census":
        census()
    else:
        raise SystemExit("用法：parse_probe.py hardware | scan <目录> | scan-db | census")
