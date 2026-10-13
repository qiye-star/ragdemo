# ruff: noqa: E501
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.parse.local.layout import PageLayout
from ragdemo.parse.textin import DocTypeAwareParser, ParseResult
from ragdemo.parse.tiered.ledger import l3_no_output_alerts
from ragdemo.parse.tiered.parser import (
    FALLBACK_ASSEMBLY,
    FALLBACK_L0,
    L0_REFUSED,
    REASON_L0_OFF,
    REASON_PERIODIC,
    TieredParser,
    tiered_fingerprint,
)
from ragdemo.parse.tiered.records import REASON_L3_NO_OUTPUT
from ragdemo_core.blob import LocalBlobStore

P3_TEXT = "股东大会审议同意本次议案，全体股东一致认为本次调整符合公司长远发展需要，不存在损害股东利益的情形。"
TEXT = {1: "营业收入 91.2 亿元。", 2: "单位：亿元 净利润 19.3", 3: P3_TEXT, 4: ""}
# 原文页 -> (段落, 表格数字)
TEXTIN: dict[int, tuple[str, str | None]] = {
    1: ("营业收入 91.2 亿元。", None),
    2: ("单位：亿元", "19.3"),
    3: (P3_TEXT, None),
    4: ("", None),
}


def _pdf(n: int) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    for i in range(1, n + 1):
        c.setFont("Helvetica", 12)
        c.drawString(60, 700, f"P{i}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _layouts(n: int, text: dict[int, str] | None = None) -> list[PageLayout]:
    t = text or TEXT
    return [PageLayout(i, 600.0, 800.0, [], t[i]) for i in range(1, n + 1)]


class FakeTextIn:
    def __init__(
        self,
        blob: LocalBlobStore,
        *,
        from_cache: bool = False,
        zero_based_missing_first: bool = False,
        no_json: bool = False,
    ) -> None:
        self.blob, self.from_cache = blob, from_cache
        self.zero_based_missing_first = zero_based_missing_first
        self.no_json = no_json
        self.calls: list[tuple[list[int], str | None]] = []

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        reader = PdfReader(io.BytesIO(file_bytes))
        originals = [int((p.extract_text() or "").strip().lstrip("P")) for p in reader.pages]
        self.calls.append((originals, owner_user))
        detail: list[dict[str, object]] = []
        blocks: list[NormalizedBlock] = []
        for i, orig in enumerate(originals, start=1):
            para, number = TEXTIN[orig]
            if para:
                detail.append(
                    {
                        "page_id": i,
                        "paragraph_id": len(detail),
                        "type": "paragraph",
                        "text": para,
                        "content": 0,
                    }
                )
                blocks.append(NormalizedBlock(len(blocks), "paragraph", "", para, page=i))
            if number is not None:
                cells = [
                    {
                        "row": 0,
                        "col": 0,
                        "row_span": 1,
                        "col_span": 1,
                        "text": "净利润",
                        "page_id": i,
                    },
                    {
                        "row": 0,
                        "col": 1,
                        "row_span": 1,
                        "col_span": 1,
                        "text": number,
                        "page_id": i,
                    },
                ]
                detail.append(
                    {
                        "page_id": i,
                        "paragraph_id": len(detail),
                        "type": "table",
                        "text": "",
                        "content": 0,
                        "cells": cells,
                    }
                )
                blocks.append(
                    NormalizedBlock(len(blocks), "table", "", f"| 净利润 | {number} |", page=i)
                )
        base = 0 if self.zero_based_missing_first else 1
        metrics = [{"page_id": i} for i in range(base, base + len(originals))]
        key = f"parse/textin/{hashlib.sha256(file_bytes).hexdigest()}/fp.json"
        payload = {"code": 200, "metrics": metrics, "result": {"detail": detail}}
        self.blob.put(key, json.dumps(payload, ensure_ascii=False).encode())
        return ParseResult(
            blocks,
            len(originals),
            "textin:1+fp",
            "",
            None if self.no_json else key,
            None,
            [],
            self.from_cache,
        )


def _parser(
    tmp_path: Path,
    *,
    skips: frozenset[str] = frozenset(),
    kinds: dict[int, str] | None = None,
    text: dict[int, str] | None = None,
    from_cache: bool = False,
    zero_based_missing_first: bool = False,
    no_json: bool = False,
) -> tuple[TieredParser, FakeTextIn, LocalBlobStore]:
    blob = LocalBlobStore(tmp_path)
    t = FakeTextIn(
        blob,
        from_cache=from_cache,
        zero_based_missing_first=zero_based_missing_first,
        no_json=no_json,
    )
    k = kinds or {}
    p = TieredParser(
        t,
        blob,
        doc_types=frozenset({"announcement", "research_report"}),
        enforced_skips=skips,  # type: ignore[arg-type]
        textin_fp="fp",
        extract=lambda b: _layouts(len(PdfReader(io.BytesIO(b)).pages), text),
        classify=lambda ls: [k.get(i, "plain_text") for i in range(1, len(ls) + 1)],
    )
    return p, t, blob


def test_the_parser_is_doc_type_aware(tmp_path: Path) -> None:
    assert isinstance(_parser(tmp_path)[0], DocTypeAwareParser)


def test_l0_refusal_message_is_fixed() -> None:
    assert L0_REFUSED == "L0 v2 not validated (G9)"


def test_without_skips_the_whole_document_goes_to_textin_unchanged(tmp_path: Path) -> None:
    p, t, _ = _parser(tmp_path)
    r = p.parse_for(_pdf(3), doc_type="announcement", title="公告")
    assert t.calls == [([1, 2, 3], None)]
    assert r.engine_version == "textin:1+fp" and {b.source for b in r.blocks} == {"textin"}
    led = r.ledger
    assert led is not None and led.routes_ref == r.json_ref
    assert {x.tier for x in led.pages} == {"L3"} and {x.reasons for x in led.pages} == {
        (REASON_L0_OFF,)
    }
    assert (led.doc.l0_pages, led.doc.l3_pages, led.doc.skipped_pages) == (0, 3, 0)
    assert r.billable_pages == 3 and led.doc.billed_pages == 3


def test_real_table_block_counts_reach_p5(tmp_path: Path) -> None:
    ok, _, _ = _parser(tmp_path / "a", kinds={2: "table"})
    r = ok.parse_for(_pdf(3), doc_type="announcement", title="公告")
    assert r.ledger is not None
    assert r.ledger.pages[1].table_blocks == 1 and r.ledger.doc.state == "accepted"
    # 第 3 页被判成表格页，但 TextIn 没给表格块：P5 不过，文档不合格。
    bad, _, _ = _parser(tmp_path / "b", kinds={3: "table"})
    r2 = bad.parse_for(_pdf(3), doc_type="announcement", title="公告")
    assert r2.ledger is not None
    assert r2.ledger.pages[2].table_blocks == 0 and r2.ledger.doc.state == "rejected"


def test_periodic_reports_go_whole(tmp_path: Path) -> None:
    p, t, _ = _parser(tmp_path)
    r = p.parse_for(_pdf(3), doc_type="announcement", title="海光信息2024年年度报告")
    assert t.calls == [([1, 2, 3], None)] and r.ledger is not None
    assert {x.tier for x in r.ledger.pages} == {"L3"} and r.ledger.pages[0].reasons == (
        REASON_PERIODIC,
    )


def test_private_material_and_untargeted_doc_types_bypass_routing(tmp_path: Path) -> None:
    p, t, _ = _parser(tmp_path)
    r1 = p.parse_for(_pdf(3), doc_type="announcement", title="x", owner_user="u1")
    r2 = p.parse_for(_pdf(3), doc_type="8-K", title="x")
    assert t.calls == [([1, 2, 3], "u1"), ([1, 2, 3], None)]
    assert (r1.ledger, r2.ledger, r1.billable_pages) == (None, None, None)


def test_layout_failure_falls_back_to_the_whole_document(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    t = FakeTextIn(blob)
    calls: list[int] = []

    def extract(_: bytes) -> list[PageLayout]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return _layouts(3)

    p = TieredParser(
        t,
        blob,
        doc_types=frozenset({"research_report"}),
        enforced_skips=frozenset(),
        textin_fp="fp",
        extract=extract,
    )
    r = p.parse_for(_pdf(3), doc_type="research_report", title="研报")
    assert t.calls == [([1, 2, 3], None)] and r.ledger is not None
    assert r.ledger.doc.fallback_reason == f"{FALLBACK_L0}：RuntimeError"


def test_enforced_skips_are_recorded_and_never_sent(tmp_path: Path) -> None:
    p, t, blob = _parser(tmp_path, skips=frozenset({"blank_page"}))
    r = p.parse_for(_pdf(4), doc_type="announcement", title="公告")
    assert t.calls == [([1, 2, 3], None)] and r.ledger is not None
    rec4 = r.ledger.pages[3]
    assert (rec4.tier, rec4.skip_rule, rec4.engine) == ("skipped", "blank_page", "skip:blank_page")
    assert (r.ledger.doc.skipped_pages, r.ledger.doc.state) == (1, "accepted")
    assert all(b.page != 4 for b in r.blocks) and [b.ordinal for b in r.blocks] == list(
        range(len(r.blocks))
    )
    assert r.engine_version == p.engine and r.engine_version.startswith("tiered:v1+")
    assert (r.page_count, r.billable_pages) == (4, 3)
    assert r.json_ref is not None and blob.exists(r.json_ref)
    side = json.loads(blob.get(r.json_ref))
    assert [x["page"] for x in side["routes"]] == [1, 2, 3]
    assert side["skipped"] == [{"page": 4, "rule": "blank_page"}]


def test_a_skipped_middle_page_splits_runs_and_maps_pages_back(tmp_path: Path) -> None:
    text = {1: TEXT[1], 2: "", 3: TEXT[3], 4: TEXT[2]}
    p, t, _ = _parser(tmp_path, skips=frozenset({"blank_page"}), text=text)
    r = p.parse_for(_pdf(4), doc_type="announcement", title="公告")
    assert t.calls == [([1], None), ([3, 4], None)]
    assert [(b.page, b.source) for b in r.blocks] == [(1, "textin"), (3, "textin")]
    assert r.ledger is not None and [x.tier for x in r.ledger.pages] == [
        "L3",
        "skipped",
        "L3",
        "L3",
    ]


def test_unenforced_matches_are_shadow_skips_and_the_page_is_parsed(tmp_path: Path) -> None:
    p, t, _ = _parser(tmp_path)
    r = p.parse_for(_pdf(4), doc_type="announcement", title="公告")
    assert t.calls == [([1, 2, 3, 4], None)] and r.ledger is not None
    assert (r.ledger.pages[3].tier, r.ledger.pages[3].shadow_skip_rules) == ("L3", ("blank_page",))


def test_cached_textin_runs_are_not_billed(tmp_path: Path) -> None:
    p, _, _ = _parser(tmp_path, from_cache=True)
    r = p.parse_for(_pdf(3), doc_type="announcement", title="公告")
    assert (r.billable_pages, r.from_cache, r.textin_calls[0].billed_pages) == (0, True, 0)
    assert r.ledger is not None and not any(x.textin_billed for x in r.ledger.pages)


def test_the_fingerprint_moves_with_skip_rules_and_pypdf_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _parser(tmp_path)[0].engine
    c = _parser(tmp_path, skips=frozenset({"blank_page"}))[0].engine
    assert a != c
    base = tiered_fingerprint(enforced=frozenset(), textin_fp="fp")
    monkeypatch.setattr("ragdemo.parse.tiered.parser.pypdf.__version__", "0.0.0")
    assert tiered_fingerprint(enforced=frozenset(), textin_fp="fp") != base


def test_periodic_doc_types_cannot_be_routed(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError, match="整份走 L3"):
        TieredParser(
            FakeTextIn(blob),
            blob,
            doc_types=frozenset({"annual_report"}),
            enforced_skips=frozenset(),
            textin_fp="fp",
        )


def test_a_page_offset_mismatch_in_a_segment_falls_back_and_still_counts_the_calls(
    tmp_path: Path,
) -> None:
    p, t, _ = _parser(tmp_path, skips=frozenset({"blank_page"}), zero_based_missing_first=True)
    r = p.parse_for(_pdf(4), doc_type="announcement", title="公告")
    assert t.calls == [([1, 2, 3], None), ([1, 2, 3, 4], None)]
    assert r.ledger is not None and (r.ledger.doc.fallback_reason or "").startswith(
        FALLBACK_ASSEMBLY
    )
    assert len(r.textin_calls) == 2 and r.billable_pages == 7
    assert any("页码基" in w for w in r.warnings)


def test_an_l3_page_without_textin_output_gets_a_reason_and_an_alert(tmp_path: Path) -> None:
    text = {**TEXT, 4: "这一页有一段足够长的文字层内容，但是 TextIn 没有给出任何条目。"}
    p, _, _ = _parser(tmp_path, text=text)
    r = p.parse_for(_pdf(4), doc_type="announcement", title="公告")
    assert r.ledger is not None
    assert REASON_L3_NO_OUTPUT in r.ledger.pages[3].reasons
    assert REASON_L3_NO_OUTPUT not in r.ledger.pages[0].reasons
    (alert,) = l3_no_output_alerts(r.ledger)
    assert alert.severity == "warning" and "4" in alert.detail


def test_an_l3_page_whose_call_has_no_json_is_flagged(tmp_path: Path) -> None:
    p, _, _ = _parser(tmp_path, no_json=True)
    r = p.parse_for(_pdf(3), doc_type="announcement", title="公告")
    assert r.ledger is not None
    assert all(REASON_L3_NO_OUTPUT in x.reasons for x in r.ledger.pages)
    assert len(l3_no_output_alerts(r.ledger)) == 1
