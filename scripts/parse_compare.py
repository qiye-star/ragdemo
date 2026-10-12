"""对比实验运行器。读库里已有的 TextIn 缓存（零 TextIn 成本），与本地路径逐文档对比。

用法（--engine llm（默认）需要 .env 里 MODEL_BASE_URL 指向出网代理的 DeepSeek 端口，
且 RAGDEMO_DSN 可用；--engine deterministic 不用模型，docs 模式只需 RAGDEMO_DSN 读库）：
    set -a && . ./.env && set +a
    # 有 TextIn 真值的已解析文档（当前 22 份）
    uv run --no-sync python scripts/parse_compare.py docs
    # 研报：无真值，只测闭合 / 状态
    uv run --no-sync python scripts/parse_compare.py research <目录>
    # 确定性排版（不调模型、无 token 用量）
    uv run --no-sync python scripts/parse_compare.py docs --engine deterministic
    uv run --no-sync python scripts/parse_compare.py research <目录> --engine deterministic

输出：data/experiments/parse-compare-<mode>-<日期>.tsv，以及逐页明细
data/experiments/parse-compare-pages-<mode>-<日期>.tsv（仅非 ok 页；data/ 已被 git 忽略）。
确定性引擎的文件名带 -det（不覆盖 llm 的结果），并把每页 markdown 写到
data/experiments/det-md/<文档>/pNNN.<状态>.md，供人工抽查版式。
"""

from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path

import psycopg

from ragdemo.config import load_config
from ragdemo.parse.describer_factory import deepseek_client
from ragdemo.parse.local.compare import (
    heading_matches,
    number_recall_precision,
    plain_text_from_markdown,
    table_row_recall,
    text_coverage,
)
from ragdemo.parse.local.det_report import (
    DET_DOCS_HEADER,
    DET_RESEARCH_HEADER,
    DetMetrics,
    PageInfo,
    band_numbers,
    classify_extra_numbers,
    det_docs_err_row,
    det_docs_row,
    det_research_err_row,
    det_research_row,
    parse_run_args,
)
from ragdemo.parse.local.layout import extract_pages
from ragdemo.parse.local.pipeline import (
    PageResult,
    parse_layouts_deterministic,
    parse_pages_local,
)
from ragdemo.parse.local.report import (
    DOCS_HEADER,
    PAGES_HEADER_STRIPPED,
    RESEARCH_HEADER,
    docs_err_row,
    docs_row,
    is_accepted,
    page_rows_stripped,
    research_err_row,
    research_row,
)
from ragdemo.parse.local.structure import LlmStructurer
from ragdemo_core.blob import LocalBlobStore

OUT_DIR = Path("data/experiments")
DOCS_SQL = (
    "SELECT doc_id, title, raw_ref, parse_md_ref, doc_type FROM core.document"
    " WHERE parse_engine LIKE 'textin:%%' AND superseded_at IS NULL"
    " AND parse_md_ref IS NOT NULL"
    " ORDER BY doc_id"
)


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
        pfh.write(PAGES_HEADER_STRIPPED + "\n")
        rows = conn.execute(DOCS_SQL).fetchall()
        for doc_id, title, raw_ref, md_ref, _doc_type in rows:
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
            for line in page_rows_stripped(str(doc_id), pages):
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
        pfh.write(PAGES_HEADER_STRIPPED + "\n")
        for pdf in sorted(directory.glob("*.pdf")):
            try:
                pages = parse_pages_local(pdf.read_bytes(), structurer)
            except Exception as exc:  # noqa: BLE001, RUF100 — 一份文档的传输错误不能中断整批实验
                fh.write(research_err_row(pdf.name, exc) + "\n")
                fh.flush()
                continue
            fh.write(research_row(pdf.name, pages) + "\n")
            for line in page_rows_stripped(pdf.name, pages):
                pfh.write(line + "\n")
            fh.flush()
            pfh.flush()
    print(_summary(structurer, out, pages_out))
    return out


def _det_pages(pdf: bytes) -> tuple[list[PageResult], list[PageInfo], float]:
    """确定性排版：版面提取 + 排版 + 自检的总耗时（秒）一并返回。"""
    t0 = time.perf_counter()
    layouts = extract_pages(pdf)
    pages = parse_layouts_deterministic(layouts)
    seconds = time.perf_counter() - t0
    infos = [PageInfo(p.markdown, band_numbers(lo)) for p, lo in zip(pages, layouts, strict=True)]
    return pages, infos, seconds


def _dump_markdown(name: str, pages: list[PageResult]) -> None:
    folder = OUT_DIR / "det-md" / re.sub(r"[^\w.-]+", "_", name)[:80]
    folder.mkdir(parents=True, exist_ok=True)
    for p in pages:
        (folder / f"p{p.page:03d}.{p.status}.md").write_text(p.markdown, encoding="utf-8")


def run_docs_det() -> Path:
    cfg = load_config()
    blob = LocalBlobStore(cfg.blob_root)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-compare-det-docs-{date.today()}.tsv"
    pages_out = OUT_DIR / f"parse-compare-det-pages-docs-{date.today()}.tsv"
    with (
        psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn,
        out.open("w", encoding="utf-8") as fh,
        pages_out.open("w", encoding="utf-8") as pfh,
    ):
        fh.write(DET_DOCS_HEADER + "\n")
        pfh.write(PAGES_HEADER_STRIPPED + "\n")
        for doc_id, title, raw_ref, md_ref, doc_type in conn.execute(DOCS_SQL).fetchall():
            try:
                reference = blob.get(str(md_ref)).decode("utf-8")
                pages, infos, seconds = _det_pages(blob.get(str(raw_ref)))
            except Exception as exc:  # noqa: BLE001, RUF100 — 一份文档的错误不能中断整批实验
                fh.write(det_docs_err_row(str(doc_id), str(title), exc) + "\n")
                fh.flush()
                continue
            _dump_markdown(str(doc_id), pages)
            metrics = None
            if is_accepted(pages):  # 与 llm 引擎一致：只对完全接受的文档算指标
                candidate = "\n\n".join(p.markdown for p in pages)
                plain = plain_text_from_markdown(reference)
                recall, precision = number_recall_precision(plain, candidate)
                metrics = DetMetrics(
                    text_coverage(plain, candidate),
                    recall,
                    precision,
                    table_row_recall(reference, candidate),
                    heading_matches(candidate, reference),
                    classify_extra_numbers(plain, infos),
                )
            fh.write(
                det_docs_row(str(doc_id), str(title), pages, metrics, seconds, str(doc_type)) + "\n"
            )
            for line in page_rows_stripped(str(doc_id), pages):
                pfh.write(line + "\n")
            fh.flush()
            pfh.flush()
    print(f"{out}\n{pages_out}\n确定性排版：不调用模型，无 token 用量")
    return out


def run_research_det(directory: Path) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parse-compare-det-research-{date.today()}.tsv"
    pages_out = OUT_DIR / f"parse-compare-det-pages-research-{date.today()}.tsv"
    with out.open("w", encoding="utf-8") as fh, pages_out.open("w", encoding="utf-8") as pfh:
        fh.write(DET_RESEARCH_HEADER + "\n")
        pfh.write(PAGES_HEADER_STRIPPED + "\n")
        for pdf in sorted(directory.glob("*.pdf")):
            try:
                pages, _, seconds = _det_pages(pdf.read_bytes())
            except Exception as exc:  # noqa: BLE001, RUF100 — 一份文档的错误不能中断整批实验
                fh.write(det_research_err_row(pdf.name, exc) + "\n")
                fh.flush()
                continue
            _dump_markdown(pdf.stem, pages)
            fh.write(det_research_row(pdf.name, pages, seconds) + "\n")
            for line in page_rows_stripped(pdf.name, pages):
                pfh.write(line + "\n")
            fh.flush()
            pfh.flush()
    print(f"{out}\n{pages_out}\n确定性排版：不调用模型，无 token 用量")
    return out


if __name__ == "__main__":
    import sys

    args = parse_run_args(sys.argv[1:])
    if args.mode == "docs":
        run_docs_det() if args.engine == "deterministic" else run_docs()
    else:
        assert args.directory is not None
        if args.engine == "deterministic":
            run_research_det(args.directory)
        else:
            run_research(args.directory)
