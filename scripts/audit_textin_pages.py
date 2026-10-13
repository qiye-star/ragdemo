"""W2：对库内已由 TextIn 解析的现行文档做逐页数字审计（零成本，只读）。

用法：
    set -a && . ./.env && set +a && uv run --no-sync python scripts/audit_textin_pages.py

只读库（SELECT）、只读 blob（原 PDF + 缓存的 TextIn JSON），不调 TextIn / LLM / 嵌入。
现行版 = core.document 中 parse_engine LIKE 'textin%' 且 superseded_at IS NULL 的行
（每个 content_hash 恰好一条；被取代的旧版是同一缓存 JSON 的重新切块，不重复审计）。
输出：data/experiments/textin-page-audit-<日期>.tsv（逐页）与 -docs.tsv（逐文档），均被 git 忽略。
TSV 与终端只含计数与数字取值，不含正文。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from ragdemo.config import load_config
from ragdemo.parse.acceptance import (
    DocumentMeta,
    PageCheck,
    PageInput,
    check_page,
    verdict_for_document,
)
from ragdemo.parse.local.chart import strip_charts
from ragdemo.parse.local.layout import PageLayout, extract_pages
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.typeset import body_font_size
from ragdemo.parse.textin_audit import (
    CAT_ADJACENT_PAGE,
    CAT_FORMAT_VARIANT,
    CAT_IMAGE_OCR,
    CAT_LIST_MERGE,
    CAT_MERGE_EXPANSION,
    CAT_SPACING,
    CAT_TITLE_YEAR,
    CAT_VALUE_ABSENT,
    PageStore,
    box_px_to_pt,
    classify_invented,
    examples,
    excess,
    heal_spacing,
    page_offset,
    split_detail_by_page,
    split_lost,
)
from ragdemo_core.blob import LocalBlobStore

STATES = (
    "accepted",
    "accepted_with_scan",
    "partially_accepted",
    "textin_unverified",
    "rejected",
    "empty_document",
)
LEGACY_REASON = "legacy_textin_full_doc"
PAGE_HEADER = (
    "doc_id\tcontent_hash12\tpage\tp3_state\tlayer_chars\tstored_items\tstored_numbers"
    "\tlayer_numbers\tinvented\tinvented_old_expanded\tinvented_examples\tinv_merge_expansion\tinv_format_variant"
    "\tinv_spacing\tinv_image_ocr\tinv_adjacent_page\tinv_title_year\tinv_list_merge\tinv_value_absent\tvalue_absent_examples\thf_numbers\tlost_total\tlost_header_footer"
    "\tlost_chart\tlost_table\tlost_other"
)
DOC_HEADER = (
    "doc_id\tcontent_hash12\tdoc_type\tpages\tverdict\tverdict_old_expanded\tverdict_healed\tinvented\thard_invented\timage_ocr\tscan_pages"
    "\tpages_with_invented\tlost_total\tlost_other"
)


def _chart_text(layout: PageLayout, body: float) -> str:
    try:
        return strip_charts(layout, body).stripped_text
    except Exception:  # 图表检测对异常页不得中断审计；记为无图表文本（更保守：不会把丢失数字洗白）
        return ""


def _table_word_texts(layout: PageLayout, store: PageStore, dims: tuple[int, int]) -> list[str]:
    boxes = [
        box_px_to_pt(b, layout.width, layout.height, float(dims[0]), float(dims[1]))
        for b in store.table_boxes
    ]
    out: list[str] = []
    for w in layout.words:
        cx, cy = (w.x0 + w.x1) / 2, (w.top + w.bottom) / 2
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes):
            out.append(w.text)
    return out


def _p3(checks: list[PageCheck]) -> PageCheck:
    return next(c for c in checks if c.id == "P3")


def audit_doc(
    doc_id: int,
    content_hash: str,
    doc_type: str,
    engine: str,
    page_count: int | None,
    pdf: bytes,
    payload: dict[str, Any],
    page_rows: list[str],
) -> tuple[str, dict[str, int]]:
    layouts = extract_pages(pdf)
    body = body_font_size(layouts)
    result = payload.get("result") or {}
    detail = result.get("detail") or []
    stores, _unpaged = split_detail_by_page(detail)
    off = page_offset(detail)
    dims = {
        int(m["page_id"]) + off: (
            int(m.get("page_image_width") or 0),
            int(m.get("page_image_height") or 0),
        )
        for m in payload.get("metrics") or []
        if m.get("page_id") is not None
    }
    by_page = {lay.page: lay.text for lay in layouts}
    adjacent = {p: by_page.get(p - 1, "") + "\n" + by_page.get(p + 1, "") for p in by_page}
    inputs: list[PageInput] = []
    inputs_old: list[PageInput] = []
    inputs_heal: list[PageInput] = []
    agg: Counter[str] = Counter()
    pages_with_invented = 0
    for lay in layouts:
        st = stores.get(lay.page, PageStore())
        layer = lay.text if lay.text.strip() else None

        def mk(text: str, page: int = lay.page, layer: str | None = layer) -> PageInput:
            return PageInput(
                page=page,
                tier="L3",
                engine=engine,
                page_kind="legacy_unclassified",
                reasons=(LEGACY_REASON,),
                tier_verdict="not_run",
                text_layer_text=layer,
                output_markdown=text,
            )

        inputs_old.append(mk(st.stored_text))  # 旧口径：合并单元格展开
        inputs_heal.append(mk(heal_spacing(st.unexpanded_text)))  # 仅比较时重接空白
        pin = mk(st.unexpanded_text)  # 新口径：每个单元格只算一次
        inputs.append(pin)
        c3 = _p3(check_page(pin))
        old_c3 = _p3(check_page(inputs_old[-1]))
        out_n, lay_n = numbers_in(st.unexpanded_text), numbers_in(lay.text)
        scan = c3.state == "unverifiable_scan"
        invented = Counter() if scan else excess(out_n, lay_n)  # 扫描页无参照，不记「多余」
        if not scan:
            assert c3.count == sum(invented.values()), (doc_id, lay.page)
        cats = classify_invented(
            "" if scan else st.unexpanded_text,
            "" if scan else st.unexpanded_text,
            lay.text,
            st.image_ocr_text,
            adjacent[lay.page],
            st.image_title_text,
        )
        agg["invented_old"] += old_c3.count
        heal_c3 = _p3(check_page(inputs_heal[-1]))
        agg["pages_pass"] += c3.state == "pass"
        agg["pages_pass_old"] += old_c3.state == "pass"
        agg["pages_pass_heal"] += heal_c3.state == "pass"
        agg["invented_heal"] += heal_c3.count
        if not scan:
            agg["checkable_numbers"] += sum(out_n.values())
        if scan:
            agg["scan_numbers"] += sum(out_n.values())
            lost = {"header_footer": 0, "chart": 0, "table": 0, "other": 0}
        else:
            lost = split_lost(
                lay.text,
                st.unexpanded_text,
                st.header_footer_text,
                _chart_text(lay, body),
                _table_word_texts(lay, st, dims.get(lay.page, (0, 0))),
            )
        n_inv = sum(invented.values())
        if n_inv:
            pages_with_invented += 1
        agg["invented"] += n_inv
        agg[CAT_VALUE_ABSENT] += sum(cats[CAT_VALUE_ABSENT].values())
        agg["hard_invented"] += sum(
            sum(cats[c].values()) for c in (CAT_IMAGE_OCR, CAT_ADJACENT_PAGE, CAT_VALUE_ABSENT)
        )
        agg[CAT_MERGE_EXPANSION] += sum(cats[CAT_MERGE_EXPANSION].values())
        agg[CAT_FORMAT_VARIANT] += sum(cats[CAT_FORMAT_VARIANT].values())
        agg[CAT_IMAGE_OCR] += sum(cats[CAT_IMAGE_OCR].values())
        agg[CAT_SPACING] += sum(cats[CAT_SPACING].values())
        agg[CAT_ADJACENT_PAGE] += sum(cats[CAT_ADJACENT_PAGE].values())
        agg[CAT_TITLE_YEAR] += sum(cats[CAT_TITLE_YEAR].values())
        agg[CAT_LIST_MERGE] += sum(cats[CAT_LIST_MERGE].values())
        agg["lost_total"] += sum(lost.values())
        agg["lost_other"] += lost["other"]
        page_rows.append(
            "\t".join(
                [
                    str(doc_id),
                    content_hash[:12],
                    str(lay.page),
                    c3.state,
                    str(len(lay.text.strip())),
                    str(sum(st.item_types.values())),
                    str(sum(out_n.values())),
                    str(sum(lay_n.values())),
                    str(n_inv),
                    str(old_c3.count),
                    ",".join(examples(invented)),
                    str(sum(cats[CAT_MERGE_EXPANSION].values())),
                    str(sum(cats[CAT_FORMAT_VARIANT].values())),
                    str(sum(cats[CAT_SPACING].values())),
                    str(sum(cats[CAT_IMAGE_OCR].values())),
                    str(sum(cats[CAT_ADJACENT_PAGE].values())),
                    str(sum(cats[CAT_TITLE_YEAR].values())),
                    str(sum(cats[CAT_LIST_MERGE].values())),
                    str(sum(cats[CAT_VALUE_ABSENT].values())),
                    ",".join(examples(cats[CAT_VALUE_ABSENT])),
                    str(sum(numbers_in(st.header_footer_text).values())),
                    str(sum(lost.values())),
                    str(lost["header_footer"]),
                    str(lost["chart"]),
                    str(lost["table"]),
                    str(lost["other"]),
                ]
            )
        )
    meta = DocumentMeta(
        provider_snapshot_saved=True,  # 缓存 JSON 存在才会走到这里
        parse_engine=engine,
        known_at_unchanged=True,  # 本审计无法核对 known_at 是否被改（只读），按未改处理
        page_count=page_count,
    )
    verdict = verdict_for_document(inputs, meta)
    agg["old_state"] = STATES.index(verdict_for_document(inputs_old, meta).state)
    agg["heal_state"] = STATES.index(verdict_for_document(inputs_heal, meta).state)
    agg["pages"] = len(layouts)
    agg["scan_pages"] = verdict.scan_pages
    agg["pages_with_invented"] = pages_with_invented
    return verdict.state, dict(agg)


def main() -> None:
    today = date.today().isoformat()
    blob = LocalBlobStore(load_config().blob_root)
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT doc_id, content_hash, doc_type, parse_engine, page_count, raw_ref"
            " FROM core.document WHERE parse_engine LIKE 'textin%'"
            " AND superseded_at IS NULL AND raw_ref IS NOT NULL ORDER BY doc_id"
        ).fetchall()
    out_dir = Path("data/experiments")
    out_dir.mkdir(parents=True, exist_ok=True)
    page_rows: list[str] = [PAGE_HEADER]
    doc_rows: list[str] = [DOC_HEADER]
    verdicts: Counter[str] = Counter()
    verdicts_old: Counter[str] = Counter()
    verdicts_heal: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    docs_with_invented = docs_with_hard = 0
    for doc_id, chash, doc_type, engine, page_count, raw_ref in rows:
        fp = re.search(r"\+([0-9a-f]+)$", str(engine))
        if fp is None:
            print(f"跳过 doc {doc_id}：parse_engine 无参数指纹", file=sys.stderr)
            continue
        json_key = f"parse/textin/{chash}/{fp.group(1)}.json"
        payload = json.loads(blob.get(json_key))
        state, agg = audit_doc(
            int(doc_id),
            str(chash),
            str(doc_type),
            str(engine),
            int(page_count) if page_count is not None else None,
            blob.get(str(raw_ref)),
            payload,
            page_rows,
        )
        verdicts[state] += 1
        verdicts_old[STATES[agg["old_state"]]] += 1
        verdicts_heal[STATES[agg["heal_state"]]] += 1
        totals.update(agg)
        docs_with_invented += agg.get("invented", 0) > 0
        docs_with_hard += agg.get("hard_invented", 0) > 0
        doc_rows.append(
            "\t".join(
                [
                    str(doc_id),
                    str(chash)[:12],
                    str(doc_type),
                    str(agg["pages"]),
                    state,
                    STATES[agg["old_state"]],
                    STATES[agg["heal_state"]],
                    str(agg["invented"]),
                    str(agg["hard_invented"]),
                    str(agg[CAT_IMAGE_OCR]),
                    str(agg["scan_pages"]),
                    str(agg["pages_with_invented"]),
                    str(agg["lost_total"]),
                    str(agg["lost_other"]),
                ]
            )
        )
        print(f"doc {doc_id}: {state} pages={agg['pages']} invented={agg['invented']}", flush=True)
    (out_dir / f"textin-page-audit-{today}.tsv").write_text(
        "\n".join(page_rows) + "\n", encoding="utf-8"
    )
    (out_dir / f"textin-page-audit-{today}-docs.tsv").write_text(
        "\n".join(doc_rows) + "\n", encoding="utf-8"
    )
    print(f"\n文档 {len(rows)} 份；新口径（不展开）：{dict(verdicts)}")
    print(f"旧口径（展开）：{dict(verdicts_old)}；新口径+比较时重接空白：{dict(verdicts_heal)}")
    print(
        f"有未溯源数字的文档 {docs_with_invented}；含图片OCR/邻页/完全缺失数字的 {docs_with_hard}"
    )
    print(f"合计：{dict(totals)}")


if __name__ == "__main__":
    main()
