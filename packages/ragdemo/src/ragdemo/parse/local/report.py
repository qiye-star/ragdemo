"""实验运行器的 TSV 行构造（纯函数，无数据库/模型依赖，便于单测列数）。"""

from __future__ import annotations

from collections.abc import Sequence

from ragdemo.parse.local.pipeline import PageResult

STATUSES = ("ok", "closure_failed", "structure_failed", "no_text_layer")
DOCS_HEADER = (
    "doc_id\ttitle\tpages\tok\tclosure_failed\tstructure_failed\tno_text_layer\taccepted"
    "\tcoverage\tnum_recall\tnum_precision\ttable_row_recall"
)
RESEARCH_HEADER = "file\tpages\tok\tclosure_failed\tstructure_failed\tno_text_layer\taccepted"
PAGES_HEADER = "doc\tpage\tstatus\treason\tviolations"
PAGES_HEADER_STRIPPED = PAGES_HEADER + "\tstripped_chars"  # 运行器用：多一列被剥字符数

Metrics = tuple[float, float, float, float]


def sanitize(value: object, limit: int = 300) -> str:
    """制表符/换行会破坏 TSV 行：替换为空格，并截断。"""
    text = str(value)
    for ch in ("\t", "\r", "\n"):
        text = text.replace(ch, " ")
    return text[:limit]


def _pad(cells: Sequence[str], header: str) -> str:
    n = header.count("\t") + 1
    cells = list(cells) + [""] * (n - len(cells))
    return "\t".join(cells)


def _counts(pages: Sequence[PageResult]) -> dict[str, int]:
    return {s: sum(1 for p in pages if p.status == s) for s in STATUSES}


def is_accepted(pages: Sequence[PageResult]) -> bool:
    """所有页都 ok：Phase B 是整份文档退回 TextIn，文档接受率比页级通过率更贴近真实节省。"""
    return len(pages) > 0 and all(p.status == "ok" for p in pages)


def docs_row(doc_id: str, title: str, pages: Sequence[PageResult], metrics: Metrics | None) -> str:
    """metrics 仅对完全接受的文档计算；否则四个指标列留空，避免被平均。"""
    c = _counts(pages)
    accepted = is_accepted(pages)
    m = ["", "", "", ""]
    if accepted and metrics is not None:
        m = [f"{x:.4f}" for x in metrics]
    cells = [
        sanitize(doc_id),
        sanitize(title),
        str(len(pages)),
        *(str(c[s]) for s in STATUSES),
        str(int(accepted)),
        *m,
    ]
    return _pad(cells, DOCS_HEADER)


def docs_err_row(doc_id: str, title: str, exc: BaseException) -> str:
    return _pad(
        [sanitize(doc_id), sanitize(title), sanitize(f"ERR:{type(exc).__name__}")], DOCS_HEADER
    )


def research_row(name: str, pages: Sequence[PageResult]) -> str:
    c = _counts(pages)
    cells = [sanitize(name), str(len(pages)), *(str(c[s]) for s in STATUSES)]
    cells.append(str(int(is_accepted(pages))))
    return _pad(cells, RESEARCH_HEADER)


def research_err_row(name: str, exc: BaseException) -> str:
    return _pad([sanitize(name), sanitize(f"ERR:{type(exc).__name__}")], RESEARCH_HEADER)


def page_rows(doc: str, pages: Sequence[PageResult]) -> list[str]:
    """每个非 ok 页一行（ok 页不写），供人工区分虚警与真编造。"""
    return [
        _pad(
            [
                sanitize(doc),
                str(p.page),
                p.status,
                sanitize(p.reason),
                sanitize(p.violations),
            ],
            PAGES_HEADER,
        )
        for p in pages
        if p.status != "ok"
    ]


def page_rows_stripped(doc: str, pages: Sequence[PageResult]) -> list[str]:
    """同 page_rows，末尾多一列 stripped_chars（该页被剥离的装饰字符数，去掉空白）。"""
    failed = [p for p in pages if p.status != "ok"]
    return [
        f"{row}\t{sum(1 for ch in p.stripped_text if not ch.isspace())}"
        for row, p in zip(page_rows(doc, failed), failed, strict=True)
    ]
