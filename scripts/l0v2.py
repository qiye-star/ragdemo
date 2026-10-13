# scripts/l0v2.py
"""确定性排版第二版（L0）评估脚本。

只读库与对象存储里已有的 TextIn 缓存，不写库、不调模型、不现调 TextIn。

set -a && . ./.env && set +a   # 只需要 RAGDEMO_DSN 与 RAGDEMO_BLOB_ROOT
uv run --no-sync python scripts/l0v2.py split            # 生成拆分清单（只运行一次）
uv run --no-sync python scripts/l0v2.py fixtures         # 生成脱敏版面夹具
uv run --no-sync python scripts/l0v2.py multicol-sheet   # 渲染页图 + 多栏标注表模板
uv run --no-sync python scripts/l0v2.py headings --set tune   # 调参集标题指标（拒绝 holdout）
uv run --no-sync python scripts/l0v2.py multicol-eval --set tune [--charts on|off]   # 多栏混淆表
uv run --no-sync python scripts/l0v2.py diag-v1          # v1 数字召回缺失归因（只看调参集文档）
uv run --no-sync python scripts/l0v2.py run --set tune   # T5 全量运行（留出集一次性锁定）
uv run --no-sync python scripts/l0v2.py gates --run data/experiments/l0v2/tune-<日期> --set tune
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Final

import pdfplumber
import psycopg
from PIL import ImageDraw

from ragdemo.config import load_config
from ragdemo.parse.figures import crop_figures
from ragdemo.parse.local.chart import CHART_DETECTOR_VERSION, strip_charts
from ragdemo.parse.local.columns import page_columns
from ragdemo.parse.local.compare import (
    HeadingMatchV2,
    heading_matches_v2,
    number_recall_precision,
    plain_text_from_markdown,
    sum_heading_matches,
    wilson_interval,
)
from ragdemo.parse.local.decor import strip_decor
from ragdemo.parse.local.l0 import page_kinds, parse_layouts_l0
from ragdemo.parse.local.l0_eval import (
    C_TEXTIN_MEASURED,
    HOLDOUT2_BLOCKING,
    CostKind,
    EvalRow,
    HeadingTotals,
    L0Metrics,
    Prices,
    aggregate_multicol,
    blended_cost_per_page,
    claim_holdout,
    content_audit,
    cost_breakdown,
    cost_kind,
    evaluate_gates,
    format_report,
    is_go,
    is_go_holdout2,
    multicolumn_rates,
    page_has_table_rows,
    parse_labels,
    read_figure_labels,
    read_judgments,
    read_multicol_labels,
    read_spotcheck,
    routing_cost_inputs,
    sample_items,
    textin_page_markdowns,
    textin_page_tables,
)
from ragdemo.parse.local.layout import extract_pages
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.pipeline import parse_pages_deterministic
from ragdemo.parse.local.recall_diag import (
    MissingNumbers,
    classify_missing_numbers,
    normalize_reference_numbers,
)
from ragdemo.parse.local.samples import (
    FORCED_TUNE,
    SampleDoc,
    broker_of,
    layout_to_fixture,
    read_split,
    require_tune_set,
    research_key,
    split_samples,
    write_split,
)
from ragdemo.parse.local.typeset import body_font_size
from ragdemo.parse.textin import blocks_from_detail
from ragdemo_core.blob import LocalBlobStore

SPLIT_PATH: Final = Path("docs/10.12-plan/l0v2/split.tsv")
LABELS_PATH: Final = Path("docs/10.12-plan/l0v2/multicol-labels.tsv")
HOLDOUT_LOCK: Final = Path("docs/10.12-plan/l0v2/holdout-run.lock")
OUT_ROOT: Final = Path("data/experiments/l0v2")
RESEARCH_DIR: Final = Path("docs/rawdata/688041-hygon/research")
FIXTURE_DIR: Final = Path("tests/fixtures/layouts")  # 公告夹具，入库
LOCAL_FIXTURE_DIR: Final = OUT_ROOT / "fixtures"  # 研报夹具，只在本机（data/ 被 git 忽略）
LABELS_HEADER: Final = "set\tgroup\tkey\tpage\tlabel\tnote"
EXTRA_PAGE_SEED: Final = 20261015
DOCS_SQL: Final = (
    "SELECT doc_id, raw_ref, parse_json_ref, parse_md_ref, doc_type FROM core.document"
    " WHERE parse_engine LIKE 'textin:%%' AND superseded_at IS NULL"
    " AND parse_json_ref IS NOT NULL ORDER BY doc_id"
)
# 夹具名 → (组, 键, 页码)
FIXTURES: Final[Mapping[str, tuple[str, str, int]]] = {
    "r_20250424_minsheng_p1": ("research", "AP202504241661650341", 1),
    "r_20240819_taipingyang_p1": ("research", "AP202408191639328385", 1),
    "r_20260415_shanxi_p1": ("research", "AP202604151821234936", 1),
    "r_20250807_kaiyuan_p1": ("research", "AP202508071723109728", 1),
    "r_20250305_taipingyang_p1": ("research", "AP202503051644062530", 1),
    "a_556_p1": ("docs", "556", 1),
    "a_560_p1": ("docs", "560", 1),
    "a_563_p1": ("docs", "563", 1),
}


def _db_docs() -> dict[str, tuple[str, str, str, str]]:
    """doc_id → (raw_ref, parse_json_ref, parse_md_ref, doc_type)。"""
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        rows = conn.execute(DOCS_SQL).fetchall()
    return {str(r[0]): (str(r[1]), str(r[2]), str(r[3] or ""), str(r[4])) for r in rows}


def _research_files() -> dict[str, Path]:
    return {research_key(p.name): p for p in sorted(RESEARCH_DIR.glob("*.pdf"))}


def _pdf_bytes(
    group: str,
    key: str,
    docs: Mapping[str, tuple[str, str, str, str]],
    blob: LocalBlobStore,
    files: Mapping[str, Path],
) -> bytes:
    return blob.get(docs[key][0]) if group == "docs" else files[key].read_bytes()


def cmd_split() -> None:
    if SPLIT_PATH.exists():
        raise SystemExit(f"{SPLIT_PATH} 已存在：拆分清单只生成一次，不得重生成")
    docs = [SampleDoc("docs", k, v[3]) for k, v in _db_docs().items()]
    research = [SampleDoc("research", k, broker_of(p.name)) for k, p in _research_files().items()]
    if len(docs) != 22 or len(research) != 91:
        print(
            f"警告：docs {len(docs)} 份、research {len(research)} 份；"
            "计划写的是 22 / 91，请先核对再提交"
        )
    rows = split_samples([*docs, *research], forced_tune=FORCED_TUNE)
    write_split(rows, SPLIT_PATH)
    for group in ("docs", "research"):
        for s in ("tune", "holdout"):
            print(group, s, sum(r.doc.group == group and r.sample_set == s for r in rows))


def cmd_fixtures() -> None:
    blob = LocalBlobStore(load_config().blob_root)
    docs = _db_docs()
    files = _research_files()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, (group, key, page) in FIXTURES.items():
        stripped = strip_decor(extract_pages(_pdf_bytes(group, key, docs, blob, files)))
        layout = stripped[page - 1].layout  # 存剥离装饰后的版面：L0 实际看到的就是它
        target = LOCAL_FIXTURE_DIR if group == "research" else FIXTURE_DIR
        (target / f"{name}.json").write_text(
            json.dumps(layout_to_fixture(layout), ensure_ascii=False), encoding="utf-8"
        )
        print(f"{name}: {len(layout.words)} 词")


def _reference_by_page(blob: LocalBlobStore, json_ref: str) -> dict[int, str]:
    """TextIn 缓存 → 页码 → 该页参考 markdown（无页眉页脚；标题级数 = outline_level + 1）。"""
    detail = json.loads(blob.get(json_ref).decode("utf-8"))["result"]["detail"]
    pages: dict[int, list[str]] = {}
    for b in blocks_from_detail(detail, page_dims={}):
        if b.page is None:
            continue
        line = (
            "#" * ((b.level or 0) + 1) + " " + b.content if b.block_type == "title" else b.content
        )
        pages.setdefault(b.page, []).append(line)
    return {k: "\n\n".join(v) for k, v in pages.items()}


def cmd_headings(set_name: str) -> None:
    """调参集（announcement）标题指标：召回 / 精确率 / 相对层级一致率（G6a/b/c 口径）。
    候选 = 现行逐页确定性排版的 ok 页；留出集一律拒绝。"""
    sample_set = require_tune_set(set_name)
    keys = {
        r.doc.key
        for r in read_split(SPLIT_PATH)
        if r.doc.group == "docs" and r.doc.stratum == "announcement" and r.sample_set == sample_set
    }
    blob = LocalBlobStore(load_config().blob_root)
    per_doc: dict[str, HeadingMatchV2] = {}
    pages_ok: dict[str, int] = {}
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        rows = conn.execute(DOCS_SQL).fetchall()
    for doc_id, raw_ref, json_ref, _md, _type in rows:
        if str(doc_id) not in keys:
            continue
        ref = _reference_by_page(blob, str(json_ref))
        ok = [r for r in parse_pages_deterministic(blob.get(str(raw_ref))) if r.status == "ok"]
        pages_ok[str(doc_id)] = len(ok)
        per_doc[str(doc_id)] = sum_heading_matches(
            heading_matches_v2(r.markdown, ref.get(r.page, "")) for r in ok
        )
    print("doc\tpages\tmatched\tcandidate\treference\trel_agree\trel_total")
    for k, m in per_doc.items():
        print(
            f"{k}\t{pages_ok[k]}\t{m.matched}\t{m.cand_total}\t{m.ref_total}\t{m.rel_agree}\t{m.rel_total}"
        )
    tot = sum_heading_matches(per_doc.values())
    lo, hi = wilson_interval(tot.matched, tot.ref_total)
    print(
        f"TOTAL\t{sum(pages_ok.values())}\t{tot.matched}\t{tot.cand_total}\t{tot.ref_total}"
        f"\t{tot.rel_agree}\t{tot.rel_total}"
    )
    print(
        f"G6a recall {tot.recall:.4f} (Wilson95 [{lo:.2f}, {hi:.2f}]) >= 0.60 | "
        f"G6b precision {tot.precision:.4f} >= 0.85 | "
        f"G6c relative level agreement {tot.relative_level_agreement:.4f} >= 0.85"
    )


def _column_flags(pdf: bytes, *, charts: bool) -> dict[int, bool]:
    """剥装饰 → （charts 时）剥图表区域 → 整页多栏判定。与 parse_layouts_l0 同一次序。"""
    stripped = strip_decor(extract_pages(pdf))
    body = body_font_size([s.layout for s in stripped])
    out: dict[int, bool] = {}
    for s in stripped:
        lay = strip_charts(s.layout, body).layout if charts else s.layout
        out[s.layout.page] = page_columns(lay, body).multicolumn or any(
            not w.upright for w in lay.words
        )
    return out


def cmd_multicol_eval(set_name: str, *, charts: bool = True) -> None:
    """【仅检测器诊断】调参集多栏检测评估：对标注页跑整页列判定（含竖排单字检查），
    charts=True 时先剥图表区域再判（与 T5 parse_layouts_l0 同一次序），False 复现 T3 的口径。
    口径与计划的 G4/G5/G11 不同（计划 G4 = L0 接受的 multi 页；G5/G11 按 financial_statement →
    multicolumn → table 的页型顺序，no_text_layer 不计），数字只作诊断，T5 须用 classify_page /
    parse_layouts_l0 重测。
    unsure 不进比率；留出集一律拒绝。另报调参集公告判多栏页占比（G11 类比）。"""
    sample_set = require_tune_set(set_name)
    split = read_split(SPLIT_PATH)
    tune_keys = {(r.doc.group, r.doc.key) for r in split if r.sample_set == sample_set}
    labels = [
        r
        for r in parse_labels(LABELS_PATH.read_text(encoding="utf-8"))
        if r.sample_set == sample_set and (r.group, r.key) in tune_keys
    ]
    blob = LocalBlobStore(load_config().blob_root)
    docs = _db_docs()
    files = _research_files()
    flags: dict[tuple[str, str], dict[int, bool]] = {}

    def run(group: str, key: str) -> dict[int, bool]:
        if (group, key) not in flags:
            flags[(group, key)] = _column_flags(
                _pdf_bytes(group, key, docs, blob, files), charts=charts
            )
        return flags[(group, key)]

    rows = [
        EvalRow(lb.group, lb.key, lb.page, lb.label, run(lb.group, lb.key)[lb.page])
        for lb in labels
    ]
    print(f"[detector-only diagnostic] charts={'on' if charts else 'off'}")
    print(format_report(aggregate_multicol(rows)))
    ann = [
        r.doc.key
        for r in split
        if r.doc.group == "docs" and r.doc.stratum == "announcement" and r.sample_set == sample_set
    ]
    pages = [flag for k in ann for flag in run("docs", k).values()]
    print(
        "[detector-only diagnostic] column-flagged announcement pages (G11 analogue): "
        f"{sum(pages)} / {len(pages)} = {sum(pages) / len(pages):.4f} (target <= 0.10)"
    )


V1_DOCS_TSV: Final = Path("data/experiments/parse-compare-det-docs-2026-10-12.tsv")
V1_MD_DIR: Final = Path("data/experiments/det-md")


def cmd_diag_v1() -> None:
    """解释 v1 的数字召回缺失：候选用 v1 运行留在本机的逐页 markdown，口径与 v1 相同
    （整份文档、只算 accepted 文档、参考 = 整份 TextIn markdown 去标签）。v1 没有图表剥离，
    figure_text 为空。**只处理调参集文档**：留出集文档一份都不读（防过拟合硬规则），
    所以不复现 v1 的全体均值 0.9855，只逐份核对 v1 记录的召回。"""
    tune_docs = {
        r.doc.key
        for r in read_split(SPLIT_PATH)
        if r.doc.group == "docs" and r.sample_set == "tune"
    }
    blob = LocalBlobStore(load_config().blob_root)
    docs = _db_docs()
    rows = [ln.split("\t") for ln in V1_DOCS_TSV.read_text(encoding="utf-8").splitlines()]
    col = {name: i for i, name in enumerate(rows[0])}
    out = OUT_ROOT / "diag-v1-recall.tsv"
    out.parent.mkdir(parents=True, exist_ok=True)
    total = MissingNumbers()
    recalls: list[float] = []
    lines = [
        "doc_id\tdoc_type\tv1_recall\trecomputed\tref\thit\tdecor\ttokenization"
        "\tnot_in_text_layer\tunexplained\texamples"
    ]
    for r in rows[1:]:
        doc_id = r[col["doc_id"]]
        if r[col["accepted"]] != "1" or doc_id not in tune_docs:
            continue
        raw_ref, _json_ref, md_ref, doc_type = docs[doc_id]
        pages = sorted((V1_MD_DIR / doc_id).glob("p*.md"))
        candidate = "\n\n".join(p.read_text(encoding="utf-8") for p in pages)
        reference = plain_text_from_markdown(blob.get(md_ref).decode("utf-8"))
        stripped = strip_decor(extract_pages(blob.get(raw_ref)))
        m = classify_missing_numbers(
            reference,
            candidate,
            layer_text="\n".join(s.layout.text for s in stripped),
            stripped_text="\n".join(s.stripped_text for s in stripped),
            layer_pages=[s.layout.text for s in stripped],
        )
        recomputed = number_recall_precision(reference, candidate)[0]
        if abs(recomputed - float(r[col["num_recall"]])) > 1e-4:
            raise SystemExit(
                f"{doc_id}：复算召回 {recomputed:.4f} != v1 记录 {r[col['num_recall']]}，"
                "v1 产物已变，停止"
            )
        total = total.plus(m)
        recalls.append(recomputed)
        lines.append(
            "\t".join(
                [
                    doc_id,
                    doc_type,
                    r[col["num_recall"]],
                    f"{recomputed:.4f}",
                    str(m.reference_total),
                    str(m.hit),
                    str(m.decor_stripped),
                    str(m.tokenization),
                    str(m.not_in_text_layer),
                    str(m.unexplained),
                    " ".join(m.examples)[:300],
                ]
            )
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}\n调参集 accepted {len(recalls)} 份，召回均值 {sum(recalls) / len(recalls):.4f}")
    print(
        f"缺失合计：decor {total.decor_stripped}、tokenization {total.tokenization}、"
        f"not_in_text_layer {total.not_in_text_layer}、unexplained {total.unexplained}"
    )


def _sheet_pages(n_pages: int, *, group: str, extra_page2: bool, rng: random.Random) -> list[int]:
    if group == "docs":
        return list(range(1, n_pages + 1))
    pages = [1]
    if n_pages > 1:
        pages.append(rng.randint(2, n_pages))
    if extra_page2 and n_pages > 1 and 2 not in pages:
        pages.append(2)
    return pages


def cmd_multicol_sheet(*, extra_page2: bool) -> None:
    """渲染页图 + 生成标注表模板（不含任何预测：盲标）。已存在的行与标签原样保留。"""
    blob = LocalBlobStore(load_config().blob_root)
    docs = _db_docs()
    files = _research_files()
    rng = random.Random(EXTRA_PAGE_SEED)
    existing: dict[tuple[str, str, str], str] = {}
    if LABELS_PATH.exists():
        for line in LABELS_PATH.read_text(encoding="utf-8").splitlines()[1:]:
            cells = line.split("\t")
            existing[(cells[1], cells[2], cells[3])] = line
    lines = [LABELS_HEADER]
    html = ["<!doctype html><meta charset='utf-8'><title>多栏标注</title>"]
    for row in read_split(SPLIT_PATH):
        g, k = row.doc.group, row.doc.key
        pdf = _pdf_bytes(g, k, docs, blob, files)
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            want_page2 = extra_page2 and row.sample_set == "holdout"
            for p in _sheet_pages(len(doc.pages), group=g, extra_page2=want_page2, rng=rng):
                img = OUT_ROOT / "pages" / g / k / f"p{p:03d}.png"
                img.parent.mkdir(parents=True, exist_ok=True)
                if not img.exists():
                    doc.pages[p - 1].to_image(resolution=60).save(img)
                key = (g, k, str(p))
                lines.append(existing.get(key, f"{row.sample_set}\t{g}\t{k}\t{p}\t\t"))
                rel = img.relative_to(OUT_ROOT / "pages").as_posix()
                html.append(f"<h3>{g} {k} 第 {p} 页</h3><img src='{rel}' style='max-width:900px'>")
    LABELS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT_ROOT / "pages" / "index.html").write_text("\n".join(html), encoding="utf-8")
    print(f"{LABELS_PATH}：{len(lines) - 1} 行；页图索引 {OUT_ROOT / 'pages' / 'index.html'}")


PAGES_HEADER: Final = (
    "group\tkey\tdoc_type\tpage\tkind\tstatus\taccepted\treason\tseconds\tfigures\tkind_legacy"
)
FIGURES_HEADER: Final = (
    "group\tkey\tdoc_type\tpage\tbox\tdetection\tx0\ttop\tx1\tbottom\twidth_px\theight_px\tdpi"
    "\tpng_bytes\tstripped_words\tcaption"
)
HEADING_SEED: Final = 20261013
SPOT_SEED: Final = 20261014
FIGURE_SEED: Final = {"holdout": 20261016, "tune": 20261017}
FIGURE_SAMPLE: Final = {"holdout": (60, 20), "tune": (30, 10)}  # (研报页, 公告页)
HEADINGS_SHEET: Final = OUT_ROOT / "heading-judgments.tsv"
SPOT_SHEET: Final = OUT_ROOT / "spotcheck.tsv"
FIGURE_BLOB: Final = OUT_ROOT / "figure-blob"  # 实验裁图只写本机实验目录，不写生产 blob_root
FIGURE_LABELS: Final = Path("docs/10.12-plan/l0v2/figure-labels.tsv")
FIGURE_REVIEW: Final = OUT_ROOT / "figures-review"
HOLDOUT2_PATH: Final = Path("docs/10.12-plan/l0v2/holdout2.tsv")
HOLDOUT2_LOCK: Final = Path("docs/10.12-plan/l0v2/holdout2-run.lock")
# 一次性运行锁：留出集（holdout）与确认集（holdout2）各一把，第二次运行一律拒绝
RUN_LOCKS: Final[Mapping[str, Path]] = {"holdout": HOLDOUT_LOCK, "holdout2": HOLDOUT2_LOCK}
RUN_SETS: Final = ("tune", "holdout", "holdout2")


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_lock(lock: Path, sample_set: str) -> None:
    """锁文件写出后立刻单独提交（只提交这一个文件），让"已经跑过"进入历史。"""
    subprocess.run(["git", "add", "--", lock.as_posix()], check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"chore(l0v2): claim the {sample_set} one-shot run lock\n\n"
            "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
            "--only",
            "--",
            lock.as_posix(),
        ],
        check=True,
    )


def _chosen_docs(sample_set: str, docs: Mapping[str, tuple[str, str, str, str]]) -> list[SampleDoc]:
    if sample_set == "holdout2":
        by_raw = {v[0]: k for k, v in docs.items()}
        rows = [ln.split("\t") for ln in HOLDOUT2_PATH.read_text(encoding="utf-8").splitlines()]
        head = rows[0]
        chosen: list[SampleDoc] = []
        for r in rows[1:]:
            rec = dict(zip(head, r, strict=True))
            if rec["raw_ref"] in by_raw:  # 只含已解析的（9 / 20 篇）；key 是解析后的新 doc_id
                chosen.append(SampleDoc("docs", by_raw[rec["raw_ref"]], rec["stratum"]))
        return chosen
    return [r.doc for r in read_split(SPLIT_PATH) if r.sample_set == sample_set]


def cmd_run(sample_set: str) -> Path:
    if sample_set not in RUN_SETS:
        raise SystemExit(f"未知样本集 {sample_set!r}")
    sha = _git_sha()
    if sample_set in RUN_LOCKS:  # 先占锁、后读任何数据：第二次运行在这里就被拒绝
        claim_holdout(RUN_LOCKS[sample_set], git_sha=sha, today=date.today())
        _commit_lock(RUN_LOCKS[sample_set], sample_set)
    out = OUT_ROOT / f"{sample_set}-{date.today()}"
    blob = LocalBlobStore(load_config().blob_root)
    fig_blob = LocalBlobStore(FIGURE_BLOB)
    docs = _db_docs()
    chosen = _chosen_docs(sample_set, docs)
    files = _research_files()
    seconds: list[float] = []
    counts: dict[str, Counter[CostKind]] = {}
    kind_counts: dict[str, Counter[str]] = {}
    closure = audit_bad = leak = ann_l0 = 0
    ann_text = ann_acc = ann_multi = ann_multi_legacy = res_text = res_acc = res_heads = 0
    fig_pages_ann = table_nums = table_lost = 0
    missing = MissingNumbers()
    fig_missing = MissingNumbers()
    heading = HeadingTotals()
    out.mkdir(parents=True, exist_ok=True)
    with (
        (out / "pages.tsv").open("w", encoding="utf-8") as fh,
        (out / "figures.tsv").open("w", encoding="utf-8") as ff,
    ):
        fh.write(PAGES_HEADER + "\n")
        ff.write(FIGURES_HEADER + "\n")
        for doc in chosen:
            g, k = doc.group, doc.key
            doc_type = docs[k][3] if g == "docs" else "research_report"
            pdf = _pdf_bytes(g, k, docs, blob, files)
            t0 = time.perf_counter()
            layouts = extract_pages(pdf)
            l0 = parse_layouts_l0(layouts)
            per_page = (time.perf_counter() - t0) / max(1, len(l0))  # 不含裁图渲染（G12 口径）
            legacy = page_kinds(layouts, multicolumn_first=True)  # 计划原文次序，仅并排报告
            stripped = strip_decor(layouts)
            payload = json.loads(blob.get(docs[k][1])) if g == "docs" else {}
            refs = textin_page_markdowns(payload) if g == "docs" else {}
            ref_tables = textin_page_tables(payload) if g == "docs" else {}
            regions = [f for p in l0 for f in p.figures]
            fig_refs, _fig_skips = crop_figures(
                pdf,
                regions,
                doc_sha256=hashlib.sha256(pdf).hexdigest(),
                blob=fig_blob,
                source="local",
                detector_version=CHART_DETECTOR_VERSION,
                page_sizes={x.page: (x.width, x.height) for x in layouts},
            )
            for n, r in enumerate(fig_refs, start=1):
                ff.write(
                    "\t".join(
                        [
                            g,
                            k,
                            doc_type,
                            str(r.page),
                            str(n),
                            r.detection,
                            *(f"{v:.1f}" for v in r.bbox_pt),
                            str(r.width_px),
                            str(r.height_px),
                            str(r.dpi),
                            str(r.png_bytes),
                            str(r.stripped_words),
                            r.caption.replace("\t", " ")[:80],
                        ]
                    )
                    + "\n"
                )
            cand_parts: list[str] = []
            ref_parts: list[str] = []
            for p, s, kl in zip(l0, stripped, legacy, strict=True):
                seconds.append(per_page)
                counts.setdefault(doc_type, Counter())[cost_kind(p)] += 1
                kind_counts.setdefault(f"{g}/{doc_type}", Counter())[p.kind] += 1
                text_page = int(p.kind != "no_text_layer")
                closure += int(p.kind == "plain_text" and p.result.status == "closure_failed")
                if doc_type == "announcement":
                    ann_text += text_page
                    ann_acc += int(p.accepted)
                    ann_multi += int(p.kind == "multicolumn")
                    ann_multi_legacy += int(kl == "multicolumn")
                if g == "research":
                    res_text += text_page
                    res_acc += int(p.accepted)
                if g == "docs" and p.figures and text_page and p.body is not None:
                    # G13b：表格数字不得被图表剥离带走（参考侧先规整 "5 . 83"，同 G3 口径）
                    tnums = numbers_in(normalize_reference_numbers(ref_tables.get(p.page, "")))
                    kept_nums = numbers_in(p.body.text + "\n" + s.stripped_text)
                    table_nums += sum(tnums.values())
                    table_lost += sum(((tnums - kept_nums) & numbers_in(p.figure_text)).values())
                reason = p.result.reason.replace("\t", " ")[:200]
                cells = [
                    g,
                    k,
                    doc_type,
                    str(p.page),
                    p.kind,
                    p.result.status,
                    str(int(p.accepted)),
                    reason,
                ]
                fh.write("\t".join([*cells, f"{per_page:.3f}", str(len(p.figures)), kl]) + "\n")
                if not p.accepted or p.body is None:
                    continue
                md = p.result.markdown
                md_path = out / "md" / g / k / f"p{p.page:03d}.md"
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(md, encoding="utf-8")
                body_words = [w.text for w in p.body.words]
                # 无损拆分：正文词 + 被剥图表文字 == 剥装饰后的全部词（内容字符多重集合）
                lossless = content_audit(
                    " ".join([*body_words, p.figure_text]), [w.text for w in s.layout.words]
                )
                if (
                    not content_audit(md, body_words)
                    or (numbers_in(md) - numbers_in(p.body.text))
                    or not lossless
                ):
                    audit_bad += 1
                if g == "research":
                    res_heads += sum(1 for ln in md.splitlines() if ln.startswith("#"))
                if doc_type == "announcement":
                    ref = refs.get(p.page, "")
                    m = classify_missing_numbers(
                        plain_text_from_markdown(ref),
                        md,
                        layer_text=p.body.text,
                        stripped_text=s.stripped_text,
                        figure_text=p.figure_text,
                    )
                    missing = missing.plus(m)
                    if p.figures:
                        fig_pages_ann += 1
                        fig_missing = fig_missing.plus(m)
                    ann_l0 += 1
                    leak += int(page_has_table_rows(ref))
                    cand_parts.append(md)
                    ref_parts.append(ref)
            if cand_parts:
                heading = heading.add(
                    heading_matches_v2("\n\n".join(cand_parts), "\n\n".join(ref_parts))
                )
    ordered = sorted(seconds)
    l1_off = Prices(c_textin=C_TEXTIN_MEASURED)
    all_counts: Counter[CostKind] = Counter()
    for c in counts.values():
        all_counts.update(c)
    cost = {
        t: {
            "counts": dict(c),
            "l1_off_cost_per_page": blended_cost_per_page(c, l1_off),
            "breakdown": cost_breakdown(c, l1_off),
            "routing_inputs": routing_cost_inputs(c),
        }
        for t, c in {**counts, "ALL": all_counts}.items()
    }
    metrics = {
        "sample_set": sample_set,
        "git_sha": sha,
        "date": date.today().isoformat(),
        "closure_failed": closure,
        "audit_inconsistent": audit_bad,
        "missing": asdict(missing),
        "num_recall": missing.recall,
        "heading": asdict(heading),
        "leakage_rate": leak / ann_l0 if ann_l0 else 0.0,
        "coverage_announcement": ann_acc / ann_text if ann_text else 0.0,
        "coverage_research": res_acc / res_text if res_text else 0.0,
        "multicol_share_announcement": ann_multi / ann_text if ann_text else 0.0,
        "multicol_share_announcement_legacy_order": ann_multi_legacy / ann_text
        if ann_text
        else 0.0,
        "seconds_p95": ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0.0,
        "research_heading_available": res_heads,
        "kind_counts": {k: dict(v) for k, v in kind_counts.items()},
        "cost": cost,
        "figure_pages_announcement": fig_pages_ann,
        "figure_missing": asdict(fig_missing),
        "figure_page_recall": fig_missing.recall if fig_pages_ann >= 10 else None,
        "figure_table_number_loss": table_lost / table_nums if table_nums else 0.0,
        "figures": {"count": sum(1 for _ in (out / "figures.tsv").open(encoding="utf-8")) - 1},
    }
    (out / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"{out}\n"
        + json.dumps({k: v for k, v in metrics.items() if k != "cost"}, ensure_ascii=False)
    )
    return out


def _accepted_pages(run_dir: Path) -> list[dict[str, str]]:
    rows = [
        ln.split("\t") for ln in (run_dir / "pages.tsv").read_text(encoding="utf-8").splitlines()
    ]
    head = rows[0]
    return [dict(zip(head, r, strict=True)) for r in rows[1:] if r[head.index("accepted")] == "1"]


def cmd_heading_sheet(run_dir: Path) -> None:
    items: list[tuple[str, int, int, int, str, str]] = []
    for r in _accepted_pages(run_dir):
        if r["group"] != "research":
            continue
        md_path = run_dir / "md" / "research" / r["key"] / f"p{int(r['page']):03d}.md"
        for i, ln in enumerate(md_path.read_text(encoding="utf-8").splitlines(), start=1):
            if ln.startswith("#"):
                level = len(ln) - len(ln.lstrip("#"))
                items.append(
                    (r["key"], int(r["page"]), i, level, ln[level:].strip(), md_path.as_posix())
                )
    picked = sample_items(items, 100, HEADING_SEED)
    lines = ["key\tpage\tline\tlevel\theading\tmd_path\tjudgment\tnote"]
    lines += [f"{k}\t{p}\t{i}\t{lv}\t{h}\t{m}\t\t" for k, p, i, lv, h, m in picked]
    HEADINGS_SHEET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{HEADINGS_SHEET}：{len(picked)} / {len(items)} 个标题待判定")


def cmd_spot_sheet(run_dir: Path) -> None:
    acc = _accepted_pages(run_dir)
    lines = ["group\tkey\tpage\tmd_path\torder_ok\tpara_ok\tnote"]
    for group, doc_type in (("docs", "announcement"), ("research", "research_report")):
        pool = [r for r in acc if r["group"] == group and r["doc_type"] == doc_type]
        for r in sample_items(pool, 15, SPOT_SEED):
            md = run_dir / "md" / group / r["key"] / f"p{int(r['page']):03d}.md"
            lines.append(f"{group}\t{r['key']}\t{r['page']}\t{md.as_posix()}\t\t\t")
    SPOT_SHEET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{SPOT_SHEET}：{len(lines) - 1} 页待抽查")


def _has_figure_rows(sample_set: str) -> bool:
    if not FIGURE_LABELS.exists():
        return False
    return any(
        ln.split("\t", 1)[0] == sample_set
        for ln in FIGURE_LABELS.read_text(encoding="utf-8").splitlines()[1:]
    )


def cmd_figure_sheet(run_dir: Path, sample_set: str, *, extra: bool) -> None:
    """U5：从有文字层页里随机抽页（不看检出结果），渲染"无框 / 有编号框"两张页图，追加标注表模板行。
    extra：留出集 charts_total < 30 时补抽研报 30 页（同一种子续抽，跳过已抽页）。"""
    if sample_set not in FIGURE_SAMPLE:
        raise SystemExit("figure-sheet 只支持 tune / holdout")
    recs = [
        dict(zip(PAGES_HEADER.split("\t"), ln.split("\t"), strict=True))
        for ln in (run_dir / "pages.tsv").read_text(encoding="utf-8").splitlines()[1:]
    ]
    text_pages = [r for r in recs if r["kind"] != "no_text_layer"]
    n_res, n_ann = FIGURE_SAMPLE[sample_set]
    rng_seed = FIGURE_SEED[sample_set]
    res_pool = [r for r in text_pages if r["group"] == "research"]
    ann_pool = [r for r in text_pages if r["doc_type"] == "announcement"]
    res_pick = sample_items(
        res_pool, n_res + 30, rng_seed
    )  # 总是抽 n_res + 30，补抽取后 30 个，前后一致
    picked = (
        res_pick[n_res:] if extra else [*res_pick[:n_res], *sample_items(ann_pool, n_ann, rng_seed)]
    )
    boxes: dict[tuple[str, str, int], list[tuple[float, float, float, float]]] = {}
    for ln in (run_dir / "figures.tsv").read_text(encoding="utf-8").splitlines()[1:]:
        f = dict(zip(FIGURES_HEADER.split("\t"), ln.split("\t"), strict=True))
        boxes.setdefault((f["group"], f["key"], int(f["page"])), []).append(
            (float(f["x0"]), float(f["top"]), float(f["x1"]), float(f["bottom"]))
        )
    blob = LocalBlobStore(load_config().blob_root)
    docs, files = _db_docs(), _research_files()
    review = FIGURE_REVIEW / sample_set
    review.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    html: list[str] = []
    for r in picked:
        g, k, page = r["group"], r["key"], int(r["page"])
        with pdfplumber.open(io.BytesIO(_pdf_bytes(g, k, docs, blob, files))) as pdf:
            pg = pdf.pages[page - 1]
            pg.to_image(resolution=100).save(review / f"{g}_{k}_p{page:03d}_plain.png")
            im = pg.to_image(resolution=100)
            for n, b in enumerate(boxes.get((g, k, page), []), start=1):
                im.draw_rect(b, stroke="red", stroke_width=3, fill=(0, 0, 0, 0))
                ImageDraw.Draw(im.annotated).text(
                    (b[0] * 100 / 72 + 3, b[1] * 100 / 72 + 2), str(n), fill="red"
                )
            im.save(review / f"{g}_{k}_p{page:03d}_boxes.png")
        n_boxes = len(boxes.get((g, k, page), []))
        rows.append(f"{sample_set}\t{g}\t{k}\t{page}\t{n_boxes}\t\t\t\t")
        html.append(
            f"<h3>{g} {k} p{page}（{n_boxes} 个框）</h3>"
            f"<img src='{g}_{k}_p{page:03d}_plain.png' width='48%'>"
            f"<img src='{g}_{k}_p{page:03d}_boxes.png' width='48%'>"
        )
    head = "set\tgroup\tkey\tpage\tboxes\tcharts_total\tcharts_covered\tbox_labels\tnote"
    old = (
        FIGURE_LABELS.read_text(encoding="utf-8").splitlines()[1:] if FIGURE_LABELS.exists() else []
    )
    FIGURE_LABELS.write_text("\n".join([head, *old, *rows]) + "\n", encoding="utf-8")
    (review / "index.html").write_text(
        "<meta charset='utf-8'><p>先看左图数图表，再看右图标框。</p>" + "".join(html),
        encoding="utf-8",
    )
    print(f"{FIGURE_LABELS}：追加 {len(rows)} 页；页图在 {review}")


def cmd_gates(run_dir: Path, sample_set: str) -> bool:
    m = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    rows = [
        ln.split("\t") for ln in (run_dir / "pages.tsv").read_text(encoding="utf-8").splitlines()
    ]
    head = rows[0]
    recs = [dict(zip(head, r, strict=True)) for r in rows[1:]]
    kinds = {(r["group"], r["key"], int(r["page"])): r["kind"] for r in recs}
    kinds_legacy = {(r["group"], r["key"], int(r["page"])): r["kind_legacy"] for r in recs}
    accepted = {(r["group"], r["key"], int(r["page"])): r["accepted"] == "1" for r in recs}
    labelled = LABELS_PATH.exists() and sample_set in ("tune", "holdout")  # holdout2 没有多栏标注
    labels = read_multicol_labels(LABELS_PATH, sample_set) if labelled else None
    multicol = multicolumn_rates(labels, kinds, accepted) if labels is not None else None
    multicol_legacy = (
        multicolumn_rates(labels, kinds_legacy, accepted) if labels is not None else None
    )
    final = sample_set == "holdout"
    yes = no = spot_pages = 0
    spot_fail: int | None = None
    if final and HEADINGS_SHEET.exists():
        yes, no = read_judgments(HEADINGS_SHEET)
    if final and SPOT_SHEET.exists():
        spot_pages, spot_fail = read_spotcheck(SPOT_SHEET)
    spot_available = sum(
        1
        for r in recs
        if r["accepted"] == "1" and r["doc_type"] in ("announcement", "research_report")
    )
    metrics = L0Metrics(
        closure_failed=int(m["closure_failed"]),
        audit_inconsistent=int(m["audit_inconsistent"]),
        num_recall=float(m["num_recall"]),
        unexplained_missing=int(m["missing"]["unexplained"]),
        multicol=multicol,
        heading=HeadingTotals(**m["heading"]),
        research_heading_available=int(m["research_heading_available"]),
        research_heading_judged=yes + no,
        research_heading_precision=yes / (yes + no) if yes + no else None,
        leakage_rate=float(m["leakage_rate"]),
        spot_available=spot_available,
        spot_pages=spot_pages,
        spot_order_failures=spot_fail,
        coverage_announcement=float(m["coverage_announcement"]),
        coverage_research=float(m["coverage_research"]),
        multicol_share_announcement=float(m["multicol_share_announcement"]),
        seconds_p95=float(m["seconds_p95"]),
        figure_pages_announcement=int(m["figure_pages_announcement"]),
        figure_page_recall=None
        if m["figure_page_recall"] is None
        else float(m["figure_page_recall"]),
        figure_table_number_loss=float(m["figure_table_number_loss"]),
        figure_labels=read_figure_labels(FIGURE_LABELS, sample_set)
        if sample_set in FIGURE_SAMPLE and _has_figure_rows(sample_set)
        else None,
    )
    gates = evaluate_gates(metrics, final=final)
    informational = sample_set == "holdout2"
    print("| 编号 | 门槛 | 实测 | 阈值 | 判定 |\n|---|---|---|---|---|")
    for g in gates:
        v = "—" if g.value is None else f"{g.value:.4f}"
        verdict = "通过" if g.passed else "未通过"
        if informational and g.gid not in HOLDOUT2_BLOCKING:
            verdict += "（仅报告）"
        print(f"| {g.gid} | {g.name} | {v} | {g.op} {g.threshold} | {verdict} |")
    # U-D3：G5 / G11 并排报告旧次序（计划原文：多栏先于表格），新次序为裁决口径
    legacy_share = float(m["multicol_share_announcement_legacy_order"])
    print(
        f"G5 新次序 {'—' if multicol is None else f'{multicol.fp_rate:.4f}'}"
        f"（旧次序 {'—' if multicol_legacy is None else f'{multicol_legacy.fp_rate:.4f}'}）；"
        f"G11 新次序 {metrics.multicol_share_announcement:.4f}（旧次序 {legacy_share:.4f}）"
    )
    go = is_go_holdout2(gates) if informational else is_go(gates)
    label = {"tune": "调参集", "holdout": "留出集", "holdout2": "holdout2（仅 G1/G2/G3b 阻断）"}[
        sample_set
    ]
    print(f"结论：{'GO' if go else 'NO-GO'}（{label}）")
    (run_dir / "gates.json").write_text(
        json.dumps(
            {
                "gates": [asdict(g) for g in gates],
                "g5_legacy_order": None if multicol_legacy is None else multicol_legacy.fp_rate,
                "g11_legacy_order": legacy_share,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return go


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="l0v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("split")
    sub.add_parser("fixtures")
    ms = sub.add_parser("multicol-sheet")
    ms.add_argument("--extra-page2", action="store_true", help="给留出集研报补标第 2 页")
    hd = sub.add_parser("headings")
    hd.add_argument("--set", dest="sample_set", required=True, help="只接受 tune")
    me = sub.add_parser("multicol-eval")
    me.add_argument("--set", dest="sample_set", required=True, help="只接受 tune")
    me.add_argument("--charts", choices=("on", "off"), default="on", help="先剥图表再判多栏")
    sub.add_parser("diag-v1")
    rn = sub.add_parser("run", help="全量运行；holdout / holdout2 是一次性锁定运行")
    rn.add_argument("--set", dest="sample_set", required=True, choices=RUN_SETS)
    hs = sub.add_parser("heading-sheet")
    hs.add_argument("--run", required=True, type=Path)
    ss = sub.add_parser("spot-sheet")
    ss.add_argument("--run", required=True, type=Path)
    fs = sub.add_parser("figure-sheet")
    fs.add_argument("--run", required=True, type=Path)
    fs.add_argument("--set", dest="sample_set", required=True, choices=("tune", "holdout"))
    fs.add_argument("--extra", action="store_true")
    gt = sub.add_parser("gates")
    gt.add_argument("--run", required=True, type=Path)
    gt.add_argument("--set", dest="sample_set", required=True, choices=RUN_SETS)
    args = ap.parse_args(argv)
    if args.cmd == "split":
        cmd_split()
    elif args.cmd == "fixtures":
        cmd_fixtures()
    elif args.cmd == "headings":
        try:
            cmd_headings(args.sample_set)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.cmd == "multicol-eval":
        try:
            cmd_multicol_eval(args.sample_set, charts=args.charts == "on")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.cmd == "diag-v1":
        cmd_diag_v1()
    elif args.cmd == "multicol-sheet":
        cmd_multicol_sheet(extra_page2=args.extra_page2)
    elif args.cmd == "run":
        cmd_run(args.sample_set)
    elif args.cmd == "heading-sheet":
        cmd_heading_sheet(args.run)
    elif args.cmd == "spot-sheet":
        cmd_spot_sheet(args.run)
    elif args.cmd == "figure-sheet":
        cmd_figure_sheet(args.run, args.sample_set, extra=args.extra)
    elif args.cmd == "gates":
        cmd_gates(args.run, args.sample_set)


if __name__ == "__main__":
    main()
