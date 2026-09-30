"""对比实验运行器。读库里已有的 TextIn 缓存（零 TextIn 成本），与本地路径逐文档对比。

用法（需要 .env 里 MODEL_BASE_URL 指向出网代理的 DeepSeek 端口，且 RAGDEMO_DSN 可用）：
    set -a && . ./.env && set +a
    # 有 TextIn 真值的已解析文档（当前 22 份）
    uv run --no-sync python scripts/parse_compare.py docs
    # 研报：无真值，只测闭合 / 状态
    uv run --no-sync python scripts/parse_compare.py research <目录>

输出：data/experiments/parse-compare-<mode>-<日期>.tsv（data/ 已被 git 忽略）。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import psycopg

from ragdemo.config import load_config
from ragdemo.parse.describer_factory import deepseek_client
from ragdemo.parse.local.compare import number_recall_precision, table_row_recall, text_coverage
from ragdemo.parse.local.pipeline import parse_pages_local
from ragdemo.parse.local.structure import LlmStructurer
from ragdemo_core.blob import LocalBlobStore

OUT_DIR = Path("data/experiments")


def _structurer() -> LlmStructurer:
    cfg = load_config()
    if not cfg.model_base_url:
        raise SystemExit("MODEL_BASE_URL 未设置：实验需要真实模型")
    return LlmStructurer(deepseek_client(cfg.model_base_url), model=cfg.model_name)


def run_docs() -> Path:
    cfg = load_config()
    blob = LocalBlobStore(cfg.blob_root)
    structurer = _structurer()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-compare-docs-{date.today()}.tsv"
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn, out.open("w", encoding="utf-8") as fh:
        fh.write(
            "doc_id\ttitle\tpages\tok\tclosure_failed\tstructure_failed\tno_text_layer\taccepted\tcoverage\tnum_recall\tnum_precision\ttable_row_recall\n"
        )
        rows = conn.execute(
            "SELECT doc_id, title, raw_ref, parse_md_ref FROM core.document"
            " WHERE parse_engine LIKE 'textin:%%' AND superseded_at IS NULL"
            " AND parse_md_ref IS NOT NULL"
            " ORDER BY doc_id"
        ).fetchall()
        for doc_id, title, raw_ref, md_ref in rows:
            try:
                reference = blob.get(str(md_ref)).decode("utf-8")
                pages = parse_pages_local(blob.get(str(raw_ref)), structurer)
            except Exception as exc:  # noqa: BLE001, RUF100 — 一份文档的传输错误不能中断整批实验
                fh.write(f"{doc_id}\t{title}\tERR:{type(exc).__name__}\t\t\t\t\t\t\t\t\t\n")
                fh.flush()
                continue
            candidate = "\n\n".join(p.markdown for p in pages if p.status == "ok")
            recall, precision = number_recall_precision(reference, candidate)
            n = len(pages)
            counts = {
                s: sum(1 for p in pages if p.status == s)
                for s in ("ok", "closure_failed", "structure_failed", "no_text_layer")
            }
            # accepted = 所有页都 ok。Phase B 是整份文档退回 TextIn，
            # 所以"文档接受率"比页级通过率更贴近真实节省。
            accepted = int(n > 0 and counts["ok"] == n)
            fh.write(
                f"{doc_id}\t{title}\t{n}\t{counts['ok']}\t{counts['closure_failed']}\t{counts['structure_failed']}\t{counts['no_text_layer']}\t{accepted}"
                f"\t{text_coverage(reference, candidate):.4f}\t{recall:.4f}\t{precision:.4f}"
                f"\t{table_row_recall(reference, candidate):.4f}\n"
            )
            fh.flush()
    u = structurer.usage
    print(
        f"{out}\n模型调用 {u.calls} 次，输入 {u.prompt_tokens} / 输出 {u.completion_tokens} tokens"
    )
    return out


def run_research(directory: Path) -> Path:
    structurer = _structurer()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-compare-research-{date.today()}.tsv"
    with out.open("w", encoding="utf-8") as fh:
        fh.write("file\tpages\tok\tclosure_failed\tstructure_failed\tno_text_layer\taccepted\n")
        for pdf in sorted(directory.glob("*.pdf")):
            try:
                pages = parse_pages_local(pdf.read_bytes(), structurer)
            except Exception as exc:  # noqa: BLE001, RUF100 — 一份文档的传输错误不能中断整批实验
                fh.write(f"{pdf.name}\tERR:{type(exc).__name__}\t\t\t\t\t\n")
                fh.flush()
                continue
            c = {
                s: sum(1 for p in pages if p.status == s)
                for s in ("ok", "closure_failed", "structure_failed", "no_text_layer")
            }
            accepted = int(len(pages) > 0 and c["ok"] == len(pages))
            fh.write(
                f"{pdf.name}\t{len(pages)}\t{c['ok']}\t{c['closure_failed']}\t{c['structure_failed']}\t{c['no_text_layer']}\t{accepted}\n"
            )
            fh.flush()
    u = structurer.usage
    print(
        f"{out}\n模型调用 {u.calls} 次，输入 {u.prompt_tokens} / 输出 {u.completion_tokens} tokens"
    )
    return out


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "docs"
    if mode == "docs":
        run_docs()
    elif mode == "research":
        run_research(Path(sys.argv[2]))
    else:
        raise SystemExit("用法：parse_compare.py docs | research <目录>")
