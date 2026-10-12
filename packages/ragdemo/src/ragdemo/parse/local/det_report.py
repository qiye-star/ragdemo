"""确定性排版实验的 TSV 行构造、额外数字归类、运行器参数解析（纯函数，便于单测）。

与 report.py 分开：report.py 的状态列固定为 4 种且有既有测试守着列数；
确定性排版多一种状态 layout_unsupported，所以单独一套表头。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ragdemo.parse.local.compare import HeadingMatch
from ragdemo.parse.local.decor import BOTTOM_BAND, TOP_BAND
from ragdemo.parse.local.layout import PageLayout
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.pipeline import PageResult
from ragdemo.parse.local.report import is_accepted, sanitize

DET_STATUSES = ("ok", "no_text_layer", "layout_unsupported", "closure_failed")
DET_DOCS_HEADER = (
    "doc_id\ttitle\tpages\tok\tno_text_layer\tlayout_unsupported\tclosure_failed\taccepted"
    "\tcoverage\tnum_recall\tnum_precision\ttable_row_recall"
    "\th_ref\th_cand\th_matched\th_level_agree\textra_hf\textra_table\textra_other\tseconds\tdoc_type"
)
DET_RESEARCH_HEADER = (
    "file\tpages\tok\tno_text_layer\tlayout_unsupported\tclosure_failed\taccepted\tseconds"
)
USAGE = "用法：parse_compare.py [docs | research <目录>] [--engine llm|deterministic]"


@dataclass(frozen=True)
class ExtraNumbers:
    """本地输出里比 TextIn 多出来的数字（多重集合差）按位置归类的个数。"""

    hf: int  # 落在页顶 / 页底带里（页眉页脚残留）
    table: int  # 出现在本页还原的表格行里（TextIn 把表格展开成 HTML 或省略）
    other: int


@dataclass(frozen=True)
class DetMetrics:
    coverage: float
    num_recall: float
    num_precision: float
    table_row_recall: float
    heading: HeadingMatch
    extra: ExtraNumbers


@dataclass(frozen=True)
class PageInfo:
    markdown: str
    band_numbers: Counter[str]  # 该页顶 / 底带里的数字（见 band_numbers）


def band_numbers(layout: PageLayout) -> Counter[str]:
    """顶 / 底带（与 decor 同一组常量）里直立词的数字多重集合。"""
    words = [
        w.text
        for w in layout.words
        if w.upright
        and (w.mid_y <= TOP_BAND * layout.height or w.mid_y >= (1 - BOTTOM_BAND) * layout.height)
    ]
    return numbers_in(" ".join(words))


def classify_extra_numbers(reference_plain: str, pages: Sequence[PageInfo]) -> ExtraNumbers:
    """候选（各页 markdown 的并集）比参考多出的数字，逐页归类：
    本页表格行里有 → table；否则在本页顶/底带里有 → hf；否则 other。"""
    candidate = Counter[str]()
    for p in pages:
        candidate.update(numbers_in(p.markdown))
    remaining = candidate - numbers_in(reference_plain)  # 多重集合差：只保留候选多出的部分
    counts = {"hf": 0, "table": 0, "other": 0}
    for p in pages:
        table_nums = numbers_in(
            "\n".join(ln for ln in p.markdown.splitlines() if ln.startswith("|"))
        )
        for n, c in numbers_in(p.markdown).items():
            k = min(c, remaining.get(n, 0))
            if k <= 0:
                continue
            remaining[n] -= k
            kind = "table" if n in table_nums else "hf" if n in p.band_numbers else "other"
            counts[kind] += k
    return ExtraNumbers(counts["hf"], counts["table"], counts["other"])


def _pad(cells: Sequence[str], header: str) -> str:
    n = header.count("\t") + 1
    return "\t".join([*cells, *[""] * (n - len(cells))])


def _counts(pages: Sequence[PageResult]) -> list[str]:
    return [str(sum(1 for p in pages if p.status == s)) for s in DET_STATUSES]


def det_docs_row(
    doc_id: str,
    title: str,
    pages: Sequence[PageResult],
    metrics: DetMetrics | None,
    seconds: float,
    doc_type: str = "",
) -> str:
    accepted = is_accepted(pages)
    m = [""] * 11
    if accepted and metrics is not None:
        h, e = metrics.heading, metrics.extra
        m = [
            *(
                f"{x:.4f}"
                for x in (
                    metrics.coverage,
                    metrics.num_recall,
                    metrics.num_precision,
                    metrics.table_row_recall,
                )
            ),
            *(str(x) for x in (h.ref_total, h.cand_total, h.matched, h.level_agree)),
            *(str(x) for x in (e.hf, e.table, e.other)),
        ]
    cells = [
        sanitize(doc_id),
        sanitize(title),
        str(len(pages)),
        *_counts(pages),
        str(int(accepted)),
        *m,
        f"{seconds:.2f}",
        sanitize(doc_type),
    ]
    return _pad(cells, DET_DOCS_HEADER)


def det_docs_err_row(doc_id: str, title: str, exc: BaseException) -> str:
    return _pad(
        [sanitize(doc_id), sanitize(title), sanitize(f"ERR:{type(exc).__name__}")], DET_DOCS_HEADER
    )


def det_research_row(name: str, pages: Sequence[PageResult], seconds: float) -> str:
    cells = [sanitize(name), str(len(pages)), *_counts(pages), str(int(is_accepted(pages)))]
    cells.append(f"{seconds:.2f}")
    return _pad(cells, DET_RESEARCH_HEADER)


def det_research_err_row(name: str, exc: BaseException) -> str:
    return _pad([sanitize(name), sanitize(f"ERR:{type(exc).__name__}")], DET_RESEARCH_HEADER)


Engine = Literal["llm", "deterministic"]


@dataclass(frozen=True)
class RunArgs:
    mode: Literal["docs", "research"]
    engine: Engine
    directory: Path | None


def parse_run_args(argv: Sequence[str]) -> RunArgs:
    """`docs | research <目录>`（默认 docs）加 `--engine llm|deterministic`（默认 llm）。"""
    engine: str = "llm"
    positional: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--engine":
            engine = next(it, "")
        elif arg.startswith("--engine="):
            engine = arg.removeprefix("--engine=")
        elif arg.startswith("--"):
            raise SystemExit(USAGE)
        else:
            positional.append(arg)
    if engine not in ("llm", "deterministic"):
        raise SystemExit(USAGE)
    mode = positional[0] if positional else "docs"
    if mode == "docs" and len(positional) <= 1:
        return RunArgs("docs", engine, None)  # type: ignore[arg-type]
    if mode == "research" and len(positional) == 2:
        return RunArgs("research", engine, Path(positional[1]))  # type: ignore[arg-type]
    raise SystemExit(USAGE)
