"""确定性排版审计（可复现）：对 docs（读库，需 RAGDEMO_DSN）与 research 目录重跑排版并核对溯源。

    set -a && . ./.env && set +a
    uv run --no-sync python scripts/audit_typeset.py docs/rawdata/688041-hygon/research

逐页检查（只看管线判定为 ok 的页）：
  1. 输出的内容字符（isalnum）多重集合 == 该页（剥离后）词的字符拼接；
  2. 输出的数字**顺序** == 文字层数字顺序（ordered_numbers）；
  3. 污染扫描：原始版面里有 /Rotate、文本矩阵从右向左、非直立词的页数；其中 ok 页的非直立词
     必须全部是已被剥离的无数字文字（水印），否则报污染。
最后打印汇总；有任何不一致时退出码为 1。
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

from ragdemo.config import load_config
from ragdemo.parse.local.decor import strip_decor
from ragdemo.parse.local.layout import PageLayout, extract_pages
from ragdemo.parse.local.number_closure import ordered_numbers
from ragdemo.parse.local.pipeline import parse_layouts_deterministic


def _content(text: str) -> Counter[str]:
    return Counter(ch for ch in text if ch.isalnum())


def audit(name: str, pdf: bytes, stats: Counter[str], problems: list[str]) -> None:
    layouts: list[PageLayout] = extract_pages(pdf)
    stripped = strip_decor(layouts)
    results = parse_layouts_deterministic(layouts)
    for orig, sp, res in zip(layouts, stripped, results, strict=True):
        stats["pages"] += 1
        stats[f"status:{res.status}"] += 1
        if orig.rotation:
            stats["raw:rotate_attr"] += 1
        if orig.rtl_text:
            stats["raw:rtl_text"] += 1
        rotated = [w for w in orig.words if not w.upright]
        if rotated:
            stats["raw:has_nonupright"] += 1
            if any(ch.isdigit() for w in rotated for ch in w.text):
                stats["raw:nonupright_with_digits"] += 1
        if res.status != "ok":
            continue
        stats["ok_pages_audited"] += 1
        if orig.rotation or orig.rtl_text or any(not w.upright for w in sp.layout.words):
            problems.append(f"{name} p{orig.page}: ok 页仍含旋转 / 倒向文字")
        if rotated and any(ch.isdigit() for w in rotated for ch in w.text):
            problems.append(f"{name} p{orig.page}: ok 页的旋转文字含数字")
        words = "".join(w.text for w in sp.layout.words)
        if _content(res.markdown) != _content(words):
            problems.append(f"{name} p{orig.page}: 内容字符多重集合不一致")
        if ordered_numbers(res.markdown) != ordered_numbers(sp.layout.text):
            problems.append(f"{name} p{orig.page}: 数字顺序与文字层不一致")


def main(research_dir: Path) -> int:
    stats: Counter[str] = Counter()
    problems: list[str] = []
    for pdf in sorted(research_dir.glob("*.pdf")):
        audit(pdf.name, pdf.read_bytes(), stats, problems)
    if os.environ.get("RAGDEMO_DSN"):
        import psycopg

        from ragdemo_core.blob import LocalBlobStore

        blob = LocalBlobStore(load_config().blob_root)
        with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
            rows = conn.execute(
                "SELECT doc_id, raw_ref FROM core.document"
                " WHERE parse_engine LIKE 'textin:%%' AND superseded_at IS NULL"
                " AND parse_md_ref IS NOT NULL ORDER BY doc_id"
            ).fetchall()
        for doc_id, raw_ref in rows:
            audit(f"doc-{doc_id}", blob.get(str(raw_ref)), stats, problems)
    else:
        print("RAGDEMO_DSN 未设置：跳过 docs 样本")
    for k in sorted(stats):
        print(f"{k}\t{stats[k]}")
    for line in problems:
        print("PROBLEM", line)
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法：audit_typeset.py <research 目录>")
    raise SystemExit(main(Path(sys.argv[1])))
