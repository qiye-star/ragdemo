"""逐页验收 P1-P8（docs/10.12-plan/验收路线与风险评估.md §1.3，控制者裁决 U-D1 采纳）。

纯函数：不读库、不读文件、不调接口。调用方把一页的各项输入装进 PageInput，
check_page 返回 P1-P6 六条页级检查；check_document_meta 给出文档级 P7；
verdict_for_document 汇总成 P8（DocVerdict.state，6 种结论之一）。

文档原文有歧义处，一律取保守读法（宁可误报，绝不漏报）：

1. 结论名。原文列了 accepted / accepted_with_scan / rejected 与 P3 行的「部分合格」，并要求对
   TextIn 页的无法核对数字单独处理（停止条件 S5）。这里定为六种：
   accepted、accepted_with_scan、partially_accepted（部分合格）、textin_unverified（TextIn 页
   数字未能溯源，S5 专用）、rejected、empty_document（零页）。
   优先级：empty_document > rejected > textin_unverified > partially_accepted
   > accepted_with_scan > accepted。TextIn 页的数字审计失败**绝不**落到 accepted。
2. P1「原因已记录」：L1/L2/L3 必须有至少一条降级/选路原因（为何不是 L0），L0 可无；
   PageKind、引擎标识必须非空；层级必须是 L0-L3。
3. P2：L0/L1 页必须有可用文字层且校验结论为 passed；L2 校验结论必须 passed；
   「failed」或「not_run」都算未过（失败页应已降级到更高层，仍停在原层说明没降级）。
   L3 页本身无自动校验，P2 记为通过并注明「事后审计见 P3」。
4. P3：入库文本（去 HTML 标签/注释/实体后）的数字多重集合必须是文字层数字多重集合的子集；
   多出的个数记为 unverified_numbers。无文字层（None 或全空白）记 state=unverifiable_scan，
   passed=False——不是「通过」。含 TextIn 页。output_markdown 只能含块正文，不得含 figure 的
   元数据（sha256 里有数字）。
5. P4：覆盖阈值**不预填**（原文要求先测分布）。阈值为 None 时返回 state=not_evaluated、
   passed=False（不算通过，文档层只出一条告警）。文字层内容字符（字母/数字/汉字，NFKC 折叠后）
   < 40 的页 state=na。stripped_text 只有在本页确有图表区域且 figures 非空时才从分母扣除
   （图表被剥离的文字已由 figure 块承载）。P4 只告警，不致不合格。
6. P5：page_kind 为 table / financial_statement 的页 table_blocks 须 ≥ 1，否则文档不合格。
7. P6：chart_regions > 0 的页，figures 数量须 ≥ chart_regions，且每个 figure 须有 bbox、
   64 位十六进制 png_sha256、caption 或 source_note 其一、can_show_raw 非 None。只告警。
8. P7：文档级。provider_snapshot 已存、parse_engine 指纹非空、known_at 未被改动，
   私有材料不得处于公共检索空间。meta 缺失（None）按失败处理。
9. 文档级附加（并入 P1）：页码必须无重复、无缺页（需要 meta.page_count；缺失时只告警）；
   meta.forced_l3（标题/文件名识别出的年报/半年报，识别由 W3 实现，这里只收标志）为真时
   每一页都必须是 L3，否则文档不合格。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ragdemo.parse.local.compare import plain_text_from_markdown
from ragdemo.parse.local.number_closure import fold_text, numbers_in

TIERS: Final = ("L0", "L1", "L2", "L3")
TABLE_KINDS: Final = frozenset({"table", "financial_statement"})
MIN_COVERAGE_CHARS: Final = 40
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

TierVerdict = Literal["passed", "failed", "not_run"]
CheckState = Literal["pass", "fail", "na", "unverifiable_scan", "not_evaluated"]
DocState = Literal[
    "accepted",
    "accepted_with_scan",
    "partially_accepted",
    "textin_unverified",
    "rejected",
    "empty_document",
]


@dataclass(frozen=True)
class FigureRef:
    bbox: tuple[float, float, float, float] | None
    png_sha256: str | None
    caption: str | None
    source_note: str | None
    can_show_raw: bool | None


@dataclass(frozen=True)
class PageInput:
    page: int
    tier: str  # 最终采用的层级（PageRoute.tier）
    engine: str  # PageRoute.engine
    page_kind: str  # PageKind 的取值字符串
    reasons: tuple[str, ...]  # PageRoute.reasons
    tier_verdict: TierVerdict  # 产出该页的那一级的校验结论
    text_layer_text: str | None  # 该页 PDF 文字层；无文字层为 None
    output_markdown: str  # 该页入库块正文（不含 figure 元数据）
    stripped_text: str = ""  # 因图表剥离而不在正文中的文字层文本
    table_blocks: int = 0  # 该页入库的 table 块数
    chart_regions: int = 0  # 该页检出的图表区域数
    figures: tuple[FigureRef, ...] = ()


@dataclass(frozen=True)
class DocumentMeta:
    provider_snapshot_saved: bool
    parse_engine: str
    known_at_unchanged: bool
    page_count: int | None = None
    is_private_material: bool = False
    in_public_space: bool = False
    forced_l3: bool = False  # 标题/文件名识别出的定期报告（W3 提供）


@dataclass(frozen=True)
class PageCheck:
    id: str
    passed: bool
    detail: str
    state: CheckState = "pass"
    count: int = 0  # P3: 未溯源数字个数


@dataclass(frozen=True)
class DocVerdict:
    state: DocState
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unverified_numbers: int = 0
    scan_pages: int = 0


def _ok(cid: str, detail: str, state: CheckState = "pass", count: int = 0) -> PageCheck:
    return PageCheck(cid, True, detail, state, count)


def _bad(cid: str, detail: str, state: CheckState = "fail", count: int = 0) -> PageCheck:
    return PageCheck(cid, False, detail, state, count)


def _is_textin(p: PageInput) -> bool:
    return p.tier == "L3" or p.engine.lower().startswith("textin")


def _has_text_layer(p: PageInput) -> bool:
    return p.text_layer_text is not None and bool(p.text_layer_text.strip())


def _content_len(text: str) -> int:
    return sum(1 for ch in unicodedata.normalize("NFKC", fold_text(text)) if ch.isalnum())


def _p1(p: PageInput) -> PageCheck:
    problems: list[str] = []
    if p.page < 1:
        problems.append(f"页码非法 {p.page}")
    if p.tier not in TIERS:
        problems.append(f"层级未知 {p.tier!r}")
    if not p.page_kind.strip():
        problems.append("缺 PageKind")
    if not p.engine.strip():
        problems.append("缺引擎标识")
    if p.tier in ("L1", "L2", "L3") and not any(r.strip() for r in p.reasons):
        problems.append(f"{p.tier} 未记录选路/降级原因")
    return _bad("P1", "；".join(problems)) if problems else _ok("P1", f"{p.tier} {p.page_kind}")


def _p2(p: PageInput) -> PageCheck:
    if p.tier == "L3":
        return _ok("P2", "L3 无自动校验，事后审计见 P3")
    if p.tier not in TIERS:
        return _bad("P2", "层级未知")
    if p.tier in ("L0", "L1") and not _has_text_layer(p):
        return _bad("P2", f"{p.tier} 页没有文字层，无法校验")
    if p.tier_verdict != "passed":
        return _bad("P2", f"{p.tier} 校验结论 {p.tier_verdict}，未降级")
    return _ok("P2", f"{p.tier} 校验通过")


def _p3(p: PageInput) -> PageCheck:
    if not _has_text_layer(p) or p.text_layer_text is None:
        return _bad("P3", "无文字层，数字无法核对", "unverifiable_scan")
    out = numbers_in(plain_text_from_markdown(p.output_markdown))
    src = numbers_in(p.text_layer_text)
    extra = Counter({n: c - src.get(n, 0) for n, c in out.items() if c > src.get(n, 0)})
    n = sum(extra.values())
    if n:
        return _bad("P3", f"{n} 个数字不在文字层：{sorted(extra)[:10]}", "fail", n)
    return _ok("P3", "数字均可溯源")


def _p4(p: PageInput, threshold: float | None) -> PageCheck:
    if not _has_text_layer(p) or p.text_layer_text is None:
        return _ok("P4", "无文字层", "na")
    need = _content_len(p.text_layer_text)
    if p.stripped_text and p.chart_regions > 0 and p.figures:
        need -= _content_len(p.stripped_text)
    if need < MIN_COVERAGE_CHARS:
        return _ok("P4", f"文字层内容字符 {need} < {MIN_COVERAGE_CHARS}", "na")
    if threshold is None:
        return _bad("P4", "覆盖阈值未配置", "not_evaluated")
    ratio = _content_len(plain_text_from_markdown(p.output_markdown)) / need
    if ratio < threshold:
        return _bad("P4", f"覆盖 {ratio:.3f} < {threshold}")
    return _ok("P4", f"覆盖 {ratio:.3f}")


def _p5(p: PageInput) -> PageCheck:
    if p.page_kind not in TABLE_KINDS:
        return _ok("P5", "非表格页", "na")
    if p.table_blocks < 1:
        return _bad("P5", f"{p.page_kind} 页无 table 块")
    return _ok("P5", f"{p.table_blocks} 个 table 块")


def _figure_problems(f: FigureRef) -> list[str]:
    out: list[str] = []
    if f.bbox is None:
        out.append("缺 bbox")
    if f.png_sha256 is None or not _SHA256.match(f.png_sha256):
        out.append("png_sha256 无效")
    if not ((f.caption or "").strip() or (f.source_note or "").strip()):
        out.append("缺 caption/source_note")
    if f.can_show_raw is None:
        out.append("缺 can_show_raw")
    return out


def _p6(p: PageInput) -> PageCheck:
    if p.chart_regions <= 0 and not p.figures:
        return _ok("P6", "无图表区域", "na")
    problems: list[str] = []
    if len(p.figures) < p.chart_regions:
        problems.append(f"检出 {p.chart_regions} 个图表区域，仅 {len(p.figures)} 个 figure 块")
    for i, f in enumerate(p.figures):
        problems += [f"figure[{i}] {x}" for x in _figure_problems(f)]
    return _bad("P6", "；".join(problems)) if problems else _ok("P6", f"{len(p.figures)} 个 figure")


def check_page(p: PageInput, *, coverage_threshold: float | None = None) -> list[PageCheck]:
    """P1-P6 页级检查，顺序固定。"""
    return [_p1(p), _p2(p), _p3(p), _p4(p, coverage_threshold), _p5(p), _p6(p)]


def check_document_meta(meta: DocumentMeta | None) -> PageCheck:
    """P7 文档级元数据。meta 缺失按失败。"""
    if meta is None:
        return _bad("P7", "未提供文档元数据")
    problems: list[str] = []
    if not meta.provider_snapshot_saved:
        problems.append("provider_snapshot 未存")
    if not meta.parse_engine.strip():
        problems.append("缺 parse_engine 指纹")
    if not meta.known_at_unchanged:
        problems.append("known_at 被改动")
    if meta.is_private_material and meta.in_public_space:
        problems.append("私有材料进入公共空间")
    return _bad("P7", "；".join(problems)) if problems else _ok("P7", "元数据完整")


def _structure_problems(pages: Sequence[PageInput], meta: DocumentMeta | None) -> list[str]:
    out: list[str] = []
    nums = [p.page for p in pages]
    dup = sorted(n for n, c in Counter(nums).items() if c > 1)
    if dup:
        out.append(f"页码重复 {dup}")
    if meta is not None and meta.page_count is not None:
        missing = sorted(set(range(1, meta.page_count + 1)) - set(nums))
        extra = sorted(n for n in set(nums) if n < 1 or n > meta.page_count)
        if missing:
            out.append(f"缺页 {missing[:20]}")
        if extra:
            out.append(f"页码越界 {extra[:20]}")
    if meta is not None and meta.forced_l3:
        bad = sorted(p.page for p in pages if p.tier != "L3")
        if bad:
            out.append(f"定期报告须整份 L3，非 L3 页 {bad[:20]}")
    return out


def verdict_for_document(
    pages: Sequence[PageInput],
    meta: DocumentMeta | None,
    *,
    coverage_threshold: float | None = None,
) -> DocVerdict:
    """P8：由逐页检查与 P7 汇总出文档结论。"""
    if not pages:
        return DocVerdict("empty_document", ("零页文档",))
    rejects: list[str] = _structure_problems(pages, meta)
    warnings: list[str] = []
    if meta is None or meta.page_count is None:
        warnings.append("未提供页数，无法检查缺页")
    unverified = 0
    textin_unverified = 0
    scan = 0
    p4_unconfigured = False
    for p in pages:
        cs = {c.id: c for c in check_page(p, coverage_threshold=coverage_threshold)}
        for cid in ("P1", "P2", "P5"):
            if not cs[cid].passed:
                rejects.append(f"p{p.page} {cid} {cs[cid].detail}")
        c3 = cs["P3"]
        if c3.state == "unverifiable_scan":
            scan += 1
        elif not c3.passed:
            unverified += c3.count
            if _is_textin(p):
                textin_unverified += c3.count
        c4 = cs["P4"]
        if c4.state == "not_evaluated":
            p4_unconfigured = True
        elif not c4.passed:
            warnings.append(f"p{p.page} P4 {c4.detail}")
        if not cs["P6"].passed:
            warnings.append(f"p{p.page} P6 {cs['P6'].detail}")
    if p4_unconfigured:
        warnings.append("P4 覆盖阈值未配置，未评估")
    c7 = check_document_meta(meta)
    if not c7.passed:
        rejects.append(f"P7 {c7.detail}")
    w = tuple(warnings)
    if rejects:
        return DocVerdict("rejected", tuple(rejects), w, unverified, scan)
    if textin_unverified:
        return DocVerdict(
            "textin_unverified",
            (f"TextIn 页有 {textin_unverified} 个数字不在文字层",),
            w,
            unverified,
            scan,
        )
    if unverified:
        return DocVerdict(
            "partially_accepted", (f"{unverified} 个数字不在文字层",), w, unverified, scan
        )
    if scan:
        return DocVerdict("accepted_with_scan", (f"{scan} 个扫描页无法核对数字",), w, 0, scan)
    return DocVerdict("accepted", (), w, 0, 0)
