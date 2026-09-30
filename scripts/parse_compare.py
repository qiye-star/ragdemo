"""对比实验运行器。读库里已有的 TextIn 缓存（零 TextIn 成本），与本地路径逐文档对比。

用法（需要 .env 里 MODEL_BASE_URL 指向出网代理的 DeepSeek 端口，且 RAGDEMO_DSN 可用）：
    set -a && . ./.env && set +a
    # 有 TextIn 真值的已解析文档（当前 22 份）
    uv run --no-sync python scripts/parse_compare.py docs
    # 研报：无真值，只测闭合 / 状态
    uv run --no-sync python scripts/parse_compare.py research <目录>

输出：data/experiments/parse-compare-<mode>-<日期>.tsv，以及逐页明细
data/experiments/parse-compare-pages-<mode>-<日期>.tsv（仅非 ok 页；data/ 已被 git 忽略）。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import psycopg

from ragdemo.config import load_config
from ragdemo.parse.describer_factory import deepseek_client
from ragdemo.parse.local.compare import (
    number_recall_precision,
    plain_text_from_markdown,
    table_row_recall,
    text_coverage,
)
from ragdemo.parse.local.pipeline import parse_pages_local
from ragdemo.parse.local.report import (
    DOCS_HEADER,
    PAGES_HEADER,
    RESEARCH_HEADER,
    docs_err_row,
    docs_row,
    is_accepted,
    page_rows,
    research_err_row,
    research_row,
)
from ragdemo.parse.local.structure import LlmStructurer
from ragdemo_core.blob import LocalBlobStore

OUT_DIR = Path("data/experiments")


def _structurer() -> LlmStructurer:
    cfg = load_config()
    if not cfg.model_base_url:
        raise SystemExit("MODEL_BASE_URL 未设置：实验需要真实模型")
    return LlmStructurer(deepseek_client(cfg.model_base_url), model=cfg.model_name)


def _summary(structurer: LlmStructurer, out: Path, pages_out: Path) -> str:
    u = structurer.usage
    return (
        f"{out}\n{pages_out}\n模型调用 {u.calls} 次，"
        f"输入 {u.prompt_tokens} / 输出 {u.completion_tokens} tokens"
    )


def run_docs() -> Path:
    cfg = load_config()
    blob = LocalBlobStore(cfg.blob_root)
    structurer = _structurer()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-compare-docs-{date.today()}.tsv"
    pages_out = OUT_DIR / f"parse-compare-pages-docs-{date.today()}.tsv"
    with (
        psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn,
        out.open("w", encoding="utf-8") as fh,
        pages_out.open("w", encoding="utf-8") as pfh,
    ):
        fh.write(DOCS_HEADER + "\n")
        pfh.write(PAGES_HEADER + "\n")
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
                fh.write(docs_err_row(str(doc_id), str(title), exc) + "\n")
                fh.flush()
                continue
            metrics = None
            if is_accepted(pages):  # 只对完全接受的文档算指标，避免被平均
                candidate = "\n\n".join(p.markdown for p in pages)
                plain = plain_text_from_markdown(reference)
                recall, precision = number_recall_precision(plain, candidate)
                metrics = (
                    text_coverage(plain, candidate),
                    recall,
                    precision,
                    table_row_recall(reference, candidate),  # 表格行用原始 HTML 解析
                )
            fh.write(docs_row(str(doc_id), str(title), pages, metrics) + "\n")
            for line in page_rows(str(doc_id), pages):
                pfh.write(line + "\n")
            fh.flush()
            pfh.flush()
    print(_summary(structurer, out, pages_out))
    return out


def run_research(directory: Path) -> Path:
    structurer = _structurer()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-compare-research-{date.today()}.tsv"
    pages_out = OUT_DIR / f"parse-compare-pages-research-{date.today()}.tsv"
    with out.open("w", encoding="utf-8") as fh, pages_out.open("w", encoding="utf-8") as pfh:
        fh.write(RESEARCH_HEADER + "\n")
        pfh.write(PAGES_HEADER + "\n")
        for pdf in sorted(directory.glob("*.pdf")):
            try:
                pages = parse_pages_local(pdf.read_bytes(), structurer)
            except Exception as exc:  # noqa: BLE001, RUF100 — 一份文档的传输错误不能中断整批实验
                fh.write(research_err_row(pdf.name, exc) + "\n")
                fh.flush()
                continue
            fh.write(research_row(pdf.name, pages) + "\n")
            for line in page_rows(pdf.name, pages):
                pfh.write(line + "\n")
            fh.flush()
            pfh.flush()
    print(_summary(structurer, out, pages_out))
    return out


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "docs"
    if mode == "docs":
        run_docs()
    elif mode == "research":
        run_research(Path(sys.argv[2]))
    else:
        raise SystemExit("用法：parse_compare.py docs | research <目录>")
