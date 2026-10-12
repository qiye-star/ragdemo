"""解析分级路由计划 T1 前置探测。只读：不调任何付费接口，不写库。

用法：
    uv run --no-sync python scripts/parse_probe.py hardware
    uv run --no-sync python scripts/parse_probe.py scan docs/rawdata/688041-hygon
    set -a && . ./.env && set +a && uv run --no-sync python scripts/parse_probe.py scan-db
输出：data/experiments/parse-probe-<模式>-<日期>.tsv（data/ 已被 git 忽略）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import psycopg

from ragdemo.config import load_config
from ragdemo.parse.local.layout import extract_pages
from ragdemo.parse.local.pipeline import needs_handback
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


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "hardware"
    if mode == "hardware":
        hardware()
    elif mode == "scan":
        scan_dir(Path(sys.argv[2]))
    elif mode == "scan-db":
        scan_db()
    else:
        raise SystemExit("用法：parse_probe.py hardware | scan <目录> | scan-db")
