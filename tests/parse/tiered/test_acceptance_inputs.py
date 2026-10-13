"""P1-P8 输入与逐页台账构造（L0 + TextIn 分页接入计划 T8）。"""

from __future__ import annotations

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.parse.acceptance import DocumentMeta
from ragdemo.parse.local.l0 import L0_VERSION, L0Page
from ragdemo.parse.local.pipeline import PageResult
from ragdemo.parse.textin_audit import PageStore
from ragdemo.parse.tiered.acceptance_inputs import (
    CAN_SHOW_RAW_AT_PARSE,
    RoutedPage,
    build_ledger,
    image_ocr_numbers,
    l0_page_input,
    l3_page_input,
    output_numbers,
)
from ragdemo.parse.tiered.records import ParseLedger


def _store(*texts: str, image: tuple[str, ...] = ()) -> PageStore:
    s = PageStore()
    s.unexpanded.extend(texts)
    s.stored.extend(texts)
    s.image_text.extend(image)
    s.unexpanded.extend(image)
    return s


def _l0(md: str, layer: str, stripped: str = "") -> L0Page:
    return L0Page(1, "plain_text", PageResult(1, "ok", md, layer, [], "", stripped))


def _blocks(*parts: tuple[str, str]) -> list[NormalizedBlock]:
    return [NormalizedBlock(i, t, "", c, 1) for i, (t, c) in enumerate(parts)]


def _l0_input() -> tuple[L0Page, list[NormalizedBlock]]:
    l0 = _l0("# 一、概况\n\n营业收入 91.2 亿元。", "营业收入 91.2 亿元。", stripped="第 1 页")
    return l0, _blocks(("title", "一、概况"), ("paragraph", "营业收入 91.2 亿元。"))


def test_l0_input_uses_body_blocks_only_and_is_passed() -> None:
    l0, blocks = _l0_input()
    inp = l0_page_input(l0, text_layer="营业收入 91.2 亿元。\n第 1 页", blocks=blocks, figures=())
    assert (inp.tier, inp.engine, inp.tier_verdict, inp.page_kind) == (
        "L0",
        f"local_det:{L0_VERSION}",
        "passed",
        "plain_text",
    )
    assert (
        inp.output_markdown == "一、概况\n\n营业收入 91.2 亿元。" and inp.stripped_text == "第 1 页"
    )
    assert output_numbers(inp) == 1
    assert CAN_SHOW_RAW_AT_PARSE is False


def test_l0_input_excludes_figure_blocks() -> None:
    l0, blocks = _l0_input()
    blocks.append(NormalizedBlock(9, "figure", "", "sha256 0123456789 图1", 1))
    inp = l0_page_input(l0, text_layer="营业收入 91.2 亿元。", blocks=blocks, figures=())
    assert "sha256" not in inp.output_markdown


def test_l3_input_compares_unexpanded_text_with_spacing_healed() -> None:
    inp = l3_page_input(
        2,
        engine="textin:1+fp",
        page_kind="table",
        reasons=("L0 table/layout_unsupported",),
        text_layer="增长 9.44%",
        store=_store("增长 9 . 44%"),
        table_blocks=1,
    )
    assert (
        inp.output_markdown == "增长 9.44%"
        and inp.tier_verdict == "not_run"
        and inp.table_blocks == 1
    )
    empty = l3_page_input(
        2, engine="e", page_kind="k", reasons=("r",), text_layer=None, store=None, table_blocks=0
    )
    assert empty.output_markdown == ""


def test_merged_cells_are_not_expanded_in_the_comparison_copy() -> None:
    s = PageStore()
    s.stored.extend(["100", "100"])  # 合并格展开两份
    s.unexpanded.append("100")  # 只算一次
    inp = l3_page_input(
        1,
        engine="textin:1+fp",
        page_kind="table",
        reasons=("r",),
        text_layer="100",
        store=s,
        table_blocks=1,
    )
    assert output_numbers(inp) == 1


def test_image_ocr_numbers_are_counted_separately() -> None:
    assert image_ocr_numbers(_store("正文 1", image=("60.0% 3000",))) == 2
    assert image_ocr_numbers(None) == 0


def test_build_ledger_records_every_page_and_the_document_verdict() -> None:
    l0, blocks = _l0_input()
    routed = [
        RoutedPage(
            1,
            "L0",
            "plain_text",
            l0_page_input(l0, text_layer="营业收入 91.2 亿元。", blocks=blocks, figures=()),
        ),
        RoutedPage(
            2,
            "L3",
            "table",
            l3_page_input(
                2,
                engine="textin:1+fp",
                page_kind="table",
                reasons=("L0 table/layout_unsupported",),
                text_layer="净利润 19.3",
                store=_store("净利润", "19.3"),
                table_blocks=1,
            ),
            textin_billed=True,
            textin_json_ref="j",
        ),
        RoutedPage(3, "skipped", "no_text_layer", None, skip_rule="blank_page"),
    ]
    meta = DocumentMeta(True, "tiered:v1+fp", True, page_count=3, skipped_pages=frozenset({3}))
    led = build_ledger(
        routed, routes_ref="parse/tiered/h/fp.json", meta=meta, billed_pages=1, fallback_reason=None
    )
    assert [(p.page, p.tier, p.p3_state) for p in led.pages] == [
        (1, "L0", "pass"),
        (2, "L3", "pass"),
        (3, "skipped", None),
    ]
    assert (
        led.doc.state,
        led.doc.l0_pages,
        led.doc.l3_pages,
        led.doc.skipped_pages,
        led.doc.billed_pages,
    ) == (
        "accepted",
        1,
        1,
        1,
        1,
    )
    assert led.pages[2].engine == "skip:blank_page" and led.doc.acceptance_version == "acc-v2"
    assert {c[0] for c in led.pages[0].checks} == {"P1", "P2", "P3", "P4", "P5", "P6"}


def _one_l3(text_layer: str | None, store: PageStore | None) -> ParseLedger:
    routed = [
        RoutedPage(
            1,
            "L3",
            "plain_text",
            l3_page_input(
                1,
                engine="textin:1+fp",
                page_kind="plain_text",
                reasons=("L0 关闭",),
                text_layer=text_layer,
                store=store,
                table_blocks=0,
            ),
            textin_json_ref="j",
            image_ocr_numbers=image_ocr_numbers(store),
        )
    ]
    return build_ledger(
        routed,
        routes_ref="r",
        meta=DocumentMeta(True, "e", True, page_count=1),
        billed_pages=0,
        fallback_reason=None,
    )


def test_an_extra_number_on_a_textin_page_makes_the_document_textin_unverified() -> None:
    led = _one_l3("净利润 19.3", _store("净利润 19.3 20.1"))
    p = led.pages[0]
    assert (p.p3_state, p.unverified_numbers, p.output_numbers, led.doc.state) == (
        "fail",
        1,
        2,
        "textin_unverified",
    )


def test_image_ocr_numbers_are_never_silently_accepted() -> None:
    led = _one_l3("净利润 19.3", _store("净利润 19.3", image=("60.0% 3000",)))
    p = led.pages[0]
    assert p.p3_state == "fail" and p.image_ocr_numbers == 2 and p.unverified_numbers == 2
    assert led.doc.state == "textin_unverified"
    assert any("unverifiable_image" in r for r in led.doc.reasons)


def test_scanned_page_is_unverifiable_and_never_accepted() -> None:
    led = _one_l3(None, _store("净利润 19.3"))
    p = led.pages[0]
    assert p.p3_state == "unverifiable_scan"
    assert led.doc.state == "accepted_with_scan" and led.doc.scan_pages == 1
    assert any("unverifiable_scan" in r for r in led.doc.reasons)
