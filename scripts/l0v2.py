# scripts/l0v2.py
"""确定性排版第二版（L0）评估脚本。

只读库与对象存储里已有的 TextIn 缓存，不写库、不调模型、不现调 TextIn。

set -a && . ./.env && set +a   # 只需要 RAGDEMO_DSN 与 RAGDEMO_BLOB_ROOT
uv run --no-sync python scripts/l0v2.py split            # 生成拆分清单（只运行一次）
uv run --no-sync python scripts/l0v2.py fixtures         # 生成脱敏版面夹具
uv run --no-sync python scripts/l0v2.py multicol-sheet   # 渲染页图 + 多栏标注表模板
uv run --no-sync python scripts/l0v2.py headings --set tune   # 调参集标题指标（拒绝 holdout）
uv run --no-sync python scripts/l0v2.py multicol-eval --set tune   # 调参集多栏混淆表（拒绝留出集）
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pdfplumber
import psycopg

from ragdemo.config import load_config
from ragdemo.parse.local.columns import REASON_GUTTER, REASON_VERTICAL, page_columns
from ragdemo.parse.local.compare import (
    HeadingMatchV2,
    heading_matches_v2,
    sum_heading_matches,
    wilson_interval,
)
from ragdemo.parse.local.decor import strip_decor
from ragdemo.parse.local.l0_eval import EvalRow, aggregate_multicol, format_report, parse_labels
from ragdemo.parse.local.layout import PageLayout, extract_pages
from ragdemo.parse.local.pipeline import (
    PageResult,
    parse_layouts_deterministic,
    parse_pages_deterministic,
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


def _is_column_flag(res: PageResult) -> bool:
    """流水线把这一页判为多栏退回（与 pipeline 一致：看 layout_unsupported + 多栏原因）。"""
    return res.status == "layout_unsupported" and res.reason in (REASON_GUTTER, REASON_VERTICAL)


def cmd_multicol_eval(set_name: str) -> None:
    """【仅检测器诊断】调参集多栏检测评估：对标注页跑与流水线相同的整页列判定（含竖排单字检查）。
    口径与计划的 G4/G5/G11 不同（计划 G4 = L0 接受的 multi 页；G5/G11 按 financial_statement →
    multicolumn → table 的页型顺序，no_text_layer 不计），数字只作诊断，T5 须用 classify_page /
    parse_layouts_l0 重测。
    unsure 不进比率；留出集一律拒绝。另报调参集公告页 layout_unsupported 占比（G11 类比）。"""
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
    results: dict[tuple[str, str], dict[int, PageResult]] = {}
    layouts: dict[tuple[str, str], dict[int, PageLayout]] = {}

    def run(group: str, key: str) -> dict[int, PageResult]:
        if (group, key) not in results:
            pdf = _pdf_bytes(group, key, docs, blob, files)
            pages = extract_pages(pdf)
            results[(group, key)] = {r.page: r for r in parse_layouts_deterministic(pages)}
            stripped = [s.layout for s in strip_decor(pages)]
            layouts[(group, key)] = {lay.page: lay for lay in stripped}
        return results[(group, key)]

    def detected(group: str, key: str, page: int) -> bool:
        res = run(group, key)[page]
        if _is_column_flag(res):
            return True
        if res.status in ("layout_unsupported", "no_text_layer"):
            # 流水线在进入排版（列检测）之前就因方向 / 无文字层退回了这一页：列检测器没被问到，
            # 这里直接问它（用该文档全部页的正文字号），评估的是检测器而不是上游的预先退回
            lays = list(layouts[(group, key)].values())
            return page_columns(layouts[(group, key)][page], body_font_size(lays)).multicolumn
        return False

    rows = [
        EvalRow(lb.group, lb.key, lb.page, lb.label, detected(lb.group, lb.key, lb.page))
        for lb in labels
    ]
    print(format_report(aggregate_multicol(rows)))
    ann = [
        r.doc.key
        for r in split
        if r.doc.group == "docs" and r.doc.stratum == "announcement" and r.sample_set == sample_set
    ]
    pages = [res for k in ann for res in run("docs", k).values()]
    unsupported = [res for res in pages if res.status == "layout_unsupported"]
    by_reason: dict[str, int] = {}
    for res in unsupported:
        by_reason[res.reason] = by_reason.get(res.reason, 0) + 1
    print(
        "[detector-only diagnostic] G11-analogue: "
        f"announcement pages {len(pages)}, layout_unsupported {len(unsupported)}"
        f" ({len(unsupported) / len(pages):.4f}; target <= 0.10); column-flagged "
        f"{sum(_is_column_flag(r) for r in pages)}; by reason {by_reason}"
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
            cmd_multicol_eval(args.sample_set)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.cmd == "multicol-sheet":
        cmd_multicol_sheet(extra_page2=args.extra_page2)


if __name__ == "__main__":
    main()
