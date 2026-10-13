"""把 L0 / L3 / 跳页装成 P1–P8 的输入，并构造逐页台账（纯函数）。

口径见 L0 + TextIn 分页接入计划 T8。

L0 输入是休眠的：L0 v2 未启用（G9 未过），`l0_page_input` 只供未来使用，当前路由不产生 L0 页。

TextIn 页的 P3 口径沿用 docs/10.12-plan/验收记录/TextIn页数字审计.md：
- 比较文本 = PageStore.unexpanded_text（合并格每格只算一次），再对**比较副本**做 heal_spacing；
  库里的文本与缓存 JSON 不动。
- 不做 ±1 页放宽：审计里 adjacent_page 只是归类，文档结论没有用它放行。
- 图片 OCR 数字不在文字层，P3 照常判不通过（文档为 textin_unverified），**不会被悄悄放行**；
  W1 没有单独的结论状态，所以在文档 reasons 里用 `unverifiable_image` 显式标出。
- 无文字层的扫描页 P3 为 unverifiable_scan，永不为 pass；文档 reasons 里标 `unverifiable_scan`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.parse.acceptance import (
    ACCEPTANCE_VERSION,
    DocumentMeta,
    PageInput,
    check_page,
    coverage_ratio,
    verdict_for_document,
)
from ragdemo.parse.acceptance import FigureRef as AcceptanceFigure
from ragdemo.parse.figures import FigureRef
from ragdemo.parse.local.compare import plain_text_from_markdown
from ragdemo.parse.local.l0 import L0_VERSION, L0Page
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.textin_audit import PageStore, heal_spacing
from ragdemo.parse.tiered.records import (
    DocRecord,
    LedgerTier,
    P3StateValue,
    PageRecord,
    ParseLedger,
)
from ragdemo.parse.tiered.types import SkipRuleId

CAN_SHOW_RAW_AT_PARSE: Final = False  # 解析阶段不知道来源权限，P6 取最严值


def l0_page_input(
    l0: L0Page,
    *,
    text_layer: str | None,
    blocks: Sequence[NormalizedBlock],
    figures: Sequence[FigureRef],
) -> PageInput:
    """休眠：当前没有任何调用方会产生 L0 页。"""
    body = "\n\n".join(b.content for b in blocks if b.block_type != "figure")
    return PageInput(
        page=l0.page,
        tier="L0",
        engine=f"local_det:{L0_VERSION}",
        page_kind=l0.kind,
        reasons=(),
        tier_verdict="passed" if l0.accepted else "failed",
        text_layer_text=text_layer,
        output_markdown=body,
        stripped_text=l0.result.stripped_text,
        table_blocks=0,
        chart_regions=len(l0.figures),
        figures=tuple(
            AcceptanceFigure(
                f.bbox_norm,
                f.image_sha256,
                f.caption or None,
                f.source_note or None,
                CAN_SHOW_RAW_AT_PARSE,
            )
            for f in figures
        ),
    )


def l3_page_input(
    page: int,
    *,
    engine: str,
    page_kind: str,
    reasons: tuple[str, ...],
    text_layer: str | None,
    store: PageStore | None,
    table_blocks: int,
) -> PageInput:
    output = heal_spacing(store.unexpanded_text) if store is not None else ""
    return PageInput(
        page,
        "L3",
        engine,
        page_kind,
        reasons,
        "not_run",
        text_layer,
        output,
        table_blocks=table_blocks,
    )


def image_ocr_numbers(store: PageStore | None) -> int:
    return 0 if store is None else sum(numbers_in(store.image_ocr_text).values())


def output_numbers(p: PageInput) -> int:
    return sum(numbers_in(plain_text_from_markdown(p.output_markdown)).values())


@dataclass(frozen=True)
class RoutedPage:
    page: int
    tier: LedgerTier
    page_kind: str
    input: PageInput | None  # 跳页为 None
    skip_rule: SkipRuleId | None = None
    shadow_skip_rules: tuple[SkipRuleId, ...] = ()
    textin_billed: bool = False
    textin_json_ref: str | None = None
    image_ocr_numbers: int = 0
    figure_count: int = 0


def build_ledger(
    routed: Sequence[RoutedPage],
    *,
    routes_ref: str,
    meta: DocumentMeta,
    billed_pages: int,
    fallback_reason: str | None,
) -> ParseLedger:
    records: list[PageRecord] = []
    inputs: list[PageInput] = []
    for r in sorted(routed, key=lambda x: x.page):
        if r.tier == "skipped":
            records.append(
                PageRecord(
                    r.page,
                    "skipped",
                    f"skip:{r.skip_rule}",
                    r.page_kind,
                    (f"跳页：{r.skip_rule}",),
                    skip_rule=r.skip_rule,
                    shadow_skip_rules=r.shadow_skip_rules,
                )
            )
            continue
        if r.input is None:
            raise ValueError(f"p{r.page} 是 {r.tier} 页却没有验收输入")
        checks = check_page(r.input)
        p3 = next(c for c in checks if c.id == "P3")
        records.append(
            PageRecord(
                page=r.page,
                tier=r.tier,
                engine=r.input.engine,
                page_kind=r.page_kind,
                reasons=r.input.reasons,
                shadow_skip_rules=r.shadow_skip_rules,
                textin_billed=r.textin_billed,
                textin_json_ref=r.textin_json_ref,
                tier_verdict=r.input.tier_verdict,
                p3_state=cast(P3StateValue, p3.state),
                unverified_numbers=p3.count,
                output_numbers=max(output_numbers(r.input), p3.count),
                image_ocr_numbers=r.image_ocr_numbers,
                coverage_ratio=coverage_ratio(r.input),
                table_blocks=r.input.table_blocks,
                figure_count=r.figure_count,
                chart_regions=r.input.chart_regions,
                checks=tuple((c.id, c.passed, c.state, c.detail) for c in checks),
            )
        )
        inputs.append(r.input)
    v = verdict_for_document(inputs, meta)
    extra: list[str] = []
    for rec in records:
        if rec.p3_state == "unverifiable_scan":
            extra.append(f"p{rec.page} unverifiable_scan：无文字层，数字无法核对")
        elif rec.p3_state == "fail" and rec.image_ocr_numbers > 0:
            extra.append(
                f"p{rec.page} unverifiable_image：图片 OCR 数字 {rec.image_ocr_numbers} 个"
                f"不在文字层，未经核对（未溯源共 {rec.unverified_numbers} 个）"
            )
    doc = DocRecord(
        state=v.state,
        reasons=v.reasons + tuple(extra),
        warnings=v.warnings,
        unverified_numbers=v.unverified_numbers,
        scan_pages=v.scan_pages,
        page_count=len(records),
        l0_pages=sum(1 for x in records if x.tier == "L0"),
        l3_pages=sum(1 for x in records if x.tier == "L3"),
        skipped_pages=sum(1 for x in records if x.tier == "skipped"),
        billed_pages=billed_pages,
        fallback_reason=fallback_reason,
        acceptance_version=ACCEPTANCE_VERSION,
    )
    return ParseLedger(routes_ref, tuple(records), doc)


__all__ = [
    "CAN_SHOW_RAW_AT_PARSE",
    "RoutedPage",
    "build_ledger",
    "image_ocr_numbers",
    "l0_page_input",
    "l3_page_input",
    "output_numbers",
]
