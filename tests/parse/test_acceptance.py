"""逐页验收 P1-P8（parse/acceptance.py）：每条检查的通过与失败、6 种文档结论、边界。"""

from __future__ import annotations

import dataclasses

from ragdemo.parse.acceptance import (
    DocumentMeta,
    DocVerdict,
    FigureRef,
    PageCheck,
    PageInput,
    check_document_meta,
    check_page,
    verdict_for_document,
)

TEXT = "公司 2024 年营业收入 1,234.5 万元，同比增长 12.3%。" * 3
OUT = TEXT
FIG = FigureRef(
    bbox=(1.0, 2.0, 3.0, 4.0),
    png_sha256="a" * 64,
    caption="图1 收入",
    source_note=None,
    can_show_raw=True,
)


def page(**kw: object) -> PageInput:
    base = PageInput(
        page=1,
        tier="L0",
        engine="local_l0:v2",
        page_kind="text",
        reasons=(),
        tier_verdict="passed",
        text_layer_text=TEXT,
        output_markdown=OUT,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def l3(**kw: object) -> PageInput:
    return page(
        tier="L3",
        engine="textin",
        reasons=("r",),
        tier_verdict="not_run",
        **kw,  # type: ignore[arg-type]
    )


def meta(**kw: object) -> DocumentMeta:
    base = DocumentMeta(
        provider_snapshot_saved=True,
        parse_engine="tiered:v1+abc",
        known_at_unchanged=True,
        page_count=1,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def chk(p: PageInput, cid: str, **kw: float | None) -> PageCheck:
    return {c.id: c for c in check_page(p, **kw)}[cid]  # type: ignore[arg-type]


# ---------------- 页级 ----------------


def test_clean_l0_page_passes_all() -> None:
    cs = check_page(page(), coverage_threshold=0.9)
    assert [c.id for c in cs] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert all(c.passed for c in cs), [c for c in cs if not c.passed]


def test_p1_fail_missing_kind_engine_or_reasons() -> None:
    assert not chk(page(page_kind=""), "P1").passed
    assert not chk(page(engine=""), "P1").passed
    assert not chk(page(tier="L3", engine="textin", reasons=()), "P1").passed
    assert chk(page(tier="L3", engine="textin", reasons=("financial_statement",)), "P1").passed


def test_p1_fail_unknown_tier() -> None:
    assert not chk(page(tier="L9"), "P1").passed


def test_p2_failed_verdict_not_demoted_fails() -> None:
    for t in ("L0", "L1", "L2"):
        assert not chk(page(tier=t, reasons=("x",), tier_verdict="failed"), "P2").passed
    assert not chk(page(tier_verdict="not_run"), "P2").passed


def test_p2_l3_defers_to_p3_and_l0_needs_text_layer() -> None:
    assert chk(l3(), "P2").passed
    assert not chk(page(text_layer_text=None), "P2").passed
    assert not chk(page(tier="L1", reasons=("r",), text_layer_text="  "), "P2").passed


def test_p3_numbers_subset_passes_and_extra_number_fails() -> None:
    assert chk(page(), "P3").passed
    c = chk(page(output_markdown=OUT + " 增长 99.9%"), "P3")
    assert not c.passed and c.state == "fail" and c.count == 1


def test_p3_is_multiset_not_set() -> None:
    p = page(text_layer_text="收入 100 万元", output_markdown="收入 100 万元 利润 100 万元")
    assert chk(p, "P3").count == 1


def test_p3_html_attr_numbers_not_counted() -> None:
    p = page(
        text_layer_text="项目 5",
        output_markdown='<table><tr><td colspan="7">项目</td><td>5</td></tr></table>',
    )
    assert chk(p, "P3").passed


def test_p3_scan_page_is_unverifiable_not_passed() -> None:
    p = page(tier="L2", engine="a+b", reasons=("scan",), text_layer_text=None)
    c = chk(p, "P3")
    assert not c.passed and c.state == "unverifiable_scan"


def test_p3_textin_page_is_audited() -> None:
    c = chk(l3(output_markdown=OUT + " 7777"), "P3")
    assert not c.passed and c.count == 1


def test_p4_threshold_unconfigured_is_not_a_pass() -> None:
    c = chk(page(), "P4")
    assert not c.passed and c.state == "not_evaluated"


def test_p4_low_coverage_fails_and_short_pages_skip() -> None:
    assert not chk(page(output_markdown="公司"), "P4", coverage_threshold=0.9).passed
    short = page(text_layer_text="目录", output_markdown="")
    c = chk(short, "P4", coverage_threshold=0.9)
    assert c.passed and c.state == "na"


def test_p4_stripped_text_only_excused_with_figures() -> None:
    chart = "图表标签" * 20
    full = TEXT + chart
    p = page(text_layer_text=full, output_markdown=TEXT)
    assert not chk(p, "P4", coverage_threshold=0.9).passed
    p2 = page(
        text_layer_text=full,
        output_markdown=TEXT,
        stripped_text=chart,
        chart_regions=1,
        figures=(FIG,),
    )
    assert chk(p2, "P4", coverage_threshold=0.9).passed
    p3 = page(text_layer_text=full, output_markdown=TEXT, stripped_text=chart)
    assert not chk(p3, "P4", coverage_threshold=0.9).passed


def test_p5_table_page_needs_table_block() -> None:
    for kind in ("table", "financial_statement"):
        assert not chk(page(page_kind=kind), "P5").passed
        assert chk(page(page_kind=kind, table_blocks=1), "P5").passed
    assert chk(page(page_kind="text"), "P5").state == "na"


def test_p6_figures() -> None:
    assert chk(page(), "P6").state == "na"
    assert chk(page(chart_regions=1, figures=(FIG,)), "P6").passed
    assert not chk(page(chart_regions=2, figures=(FIG,)), "P6").passed
    for bad in (
        dataclasses.replace(FIG, bbox=None),
        dataclasses.replace(FIG, png_sha256="xyz"),
        dataclasses.replace(FIG, caption=None, source_note=None),
        dataclasses.replace(FIG, can_show_raw=None),
    ):
        assert not chk(page(chart_regions=1, figures=(bad,)), "P6").passed
    ok = dataclasses.replace(FIG, caption=None, source_note="资料来源：公司")
    assert chk(page(chart_regions=1, figures=(ok,)), "P6").passed


# ---------------- P7 ----------------


def test_p7() -> None:
    assert check_document_meta(meta()).passed
    assert not check_document_meta(meta(provider_snapshot_saved=False)).passed
    assert not check_document_meta(meta(parse_engine="")).passed
    assert not check_document_meta(meta(known_at_unchanged=False)).passed
    assert not check_document_meta(meta(is_private_material=True, in_public_space=True)).passed
    assert check_document_meta(meta(is_private_material=True, in_public_space=False)).passed
    assert not check_document_meta(None).passed


# ---------------- 文档结论 P8 ----------------


def v(pages: list[PageInput], m: DocumentMeta | None = None) -> DocVerdict:
    return verdict_for_document(
        pages, m if m is not None else meta(page_count=len(pages)), coverage_threshold=0.9
    )


def test_verdict_accepted() -> None:
    d = v([page()])
    assert d.state == "accepted" and d.unverified_numbers == 0 and d.scan_pages == 0


def test_verdict_accepted_with_scan() -> None:
    scan = page(
        page=2,
        tier="L2",
        engine="a+b",
        reasons=("scan",),
        text_layer_text=None,
        output_markdown="扫描页 5",
    )
    d = v([page(), scan])
    assert d.state == "accepted_with_scan" and d.scan_pages == 1


def test_verdict_partially_accepted_unverified_numbers_non_textin() -> None:
    d = v([page(), page(page=2, output_markdown=OUT + " 4242")])
    assert d.state == "partially_accepted" and d.unverified_numbers == 1


def test_verdict_textin_unverified_is_explicit_state() -> None:
    d = v([page(), l3(page=2, output_markdown=OUT + " 4242")])
    assert d.state == "textin_unverified" and d.unverified_numbers == 1


def test_textin_unverified_outranks_scan() -> None:
    scan = page(page=3, tier="L2", engine="a+b", reasons=("s",), text_layer_text=None)
    d = v([page(), l3(page=2, output_markdown=OUT + " 4242"), scan])
    assert d.state == "textin_unverified"


def test_verdict_rejected_paths() -> None:
    assert v([page(tier_verdict="failed")]).state == "rejected"  # P2
    assert v([page(page_kind="table")]).state == "rejected"  # P5
    assert v([page(page_kind="")]).state == "rejected"  # P1
    assert v([page()], meta(provider_snapshot_saved=False)).state == "rejected"  # P7
    assert verdict_for_document([page()], None).state == "rejected"  # P7 未提供


def test_rejected_outranks_textin_unverified() -> None:
    d = v([page(page_kind="table"), l3(page=2, output_markdown=OUT + " 4242")], meta(page_count=2))
    assert d.state == "rejected"


def test_verdict_empty_document() -> None:
    assert verdict_for_document([], meta(page_count=0)).state == "empty_document"


def test_all_pages_handed_back_to_l3_can_be_accepted() -> None:
    d = v([l3(page=1), l3(page=2)], meta(page_count=2))
    assert d.state == "accepted"


def test_missing_or_duplicate_pages() -> None:
    d = v([page(page=1), l3(page=3)], meta(page_count=3))
    assert d.state == "rejected" and any("缺页" in r for r in d.reasons)
    d2 = v([page(), page()], meta(page_count=2))
    assert d2.state == "rejected"


def test_unknown_page_count_gives_warning() -> None:
    d = v([page()], meta(page_count=None))
    assert d.state == "accepted" and any("页数" in w for w in d.warnings)


def test_periodic_report_must_be_all_l3() -> None:
    m = meta(forced_l3=True)
    assert v([page()], m).state == "rejected"
    assert v([l3()], m).state == "accepted"


def test_warnings_for_p4_p6_and_unconfigured_threshold() -> None:
    d = v([page(output_markdown="公司")])
    assert d.state == "accepted" and any("P4" in w for w in d.warnings)
    d2 = verdict_for_document([page()], meta(), coverage_threshold=None)
    assert any("P4" in w and "阈值" in w for w in d2.warnings)
    badfig = page(chart_regions=1, figures=(dataclasses.replace(FIG, bbox=None),))
    assert any("P6" in w for w in v([badfig]).warnings)
