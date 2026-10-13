# ruff: noqa: E501
"""文档级入口：TextIn 连续段（L3）+ 减页；逐页台账与 P1–P8。

裁决（2026-10-13）：L0 v2 未通过 G9，**不启用**。本解析器只支持 L3 与减页规则，永远不产生 L0 页；
构造时不接受任何 L0 开关，配置层把 PARSE_TIER_L0_ENABLED=true 硬拒绝（见 config.py / factory.py）。

逐页决策：减页规则命中且已启用 → skipped（默认关；未启用的命中只作为影子跳页记台账）；其余 → L3。
没有任何跳页时整份原 PDF 送 TextIn（与旧路径同一缓存键），其余按连续段切子 PDF。
任何版面 / 规则 / 装配 / 页码基核对异常都整份退回 TextIn，不得丢文档；TextIn 自身的异常原样抛出。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Final, cast

import pypdf

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.parse.acceptance import ACCEPTANCE_VERSION, DocumentMeta
from ragdemo.parse.local.l0 import page_kinds
from ragdemo.parse.local.layout import PageLayout, extract_pages
from ragdemo.parse.textin import DocumentParser, ParseResult, param_fingerprint
from ragdemo.parse.textin_audit import PageStore, split_detail_by_page
from ragdemo.parse.tiered.acceptance_inputs import (
    RoutedPage,
    build_ledger,
    image_ocr_numbers,
    l3_page_input,
)
from ragdemo.parse.tiered.census import ALWAYS_L3_DOC_TYPES, force_whole_document_l3
from ragdemo.parse.tiered.page_offset_guard import page_offset_problem
from ragdemo.parse.tiered.records import REASON_L3_NO_OUTPUT, ParseLedger
from ragdemo.parse.tiered.skip import (
    SKIP_RULES_VERSION,
    DocKind,
    enforced_skips,
    evaluate_skip_rules,
    page_facts,
)
from ragdemo.parse.tiered.textin_pages import TextInPages, parse_pages_with_textin
from ragdemo.parse.tiered.types import (
    PageRoute,
    SkipRuleId,
    TextInCall,
    Tier,
    call_to_dict,
    route_to_dict,
)
from ragdemo_core.blob import BlobNotFound, BlobStore

_LOG = logging.getLogger(__name__)
TIERED_ALGORITHM_VERSION: Final = "v1"
L0_REFUSED: Final = "L0 v2 not validated (G9)"
REASON_L0_OFF: Final = "L0 关闭"
REASON_PERIODIC: Final = "定期报告整份 L3"
FALLBACK_L0: Final = "L0 异常整份退回"  # 版面 / 页型 / 规则阶段的任何异常（沿用计划里的名字）
FALLBACK_ASSEMBLY: Final = "装配异常整份退回"
KIND_PERIODIC: Final = "periodic_whole"
KIND_FALLBACK: Final = "fallback_unclassified"
KIND_UNCLASSIFIED: Final = "unclassified"
SIDECAR_PREFIX: Final = "parse/tiered"

PageClassifier = Callable[[Sequence[PageLayout]], list[str]]


class PageOffsetMismatch(RuntimeError):
    """TextIn 页码基与 detail 推断不一致：块页码可能整体错一页，不得入库。"""


def _table_blocks(starting_here: int, store: PageStore | None) -> int:
    """本页的表格证据数：起始于本页的表块，或本页有表格单元格（跨页表的续页）。

    跨页表只有一个块、页码是首页，续页的块数为 0；单元格自带 page_id，
    `item_types['table']` 给每个涉及的页各记一次。没有任何表格单元格的页仍是 0。
    """
    return max(starting_here, store.item_types["table"] if store else 0)


def tiered_fingerprint(*, enforced: frozenset[SkipRuleId], textin_fp: str) -> str:
    return param_fingerprint(
        {
            "algo": TIERED_ALGORITHM_VERSION,
            "l0": "off",
            "skip": f"{SKIP_RULES_VERSION}:{','.join(sorted(enforced))}",
            "acceptance": ACCEPTANCE_VERSION,
            "pypdf": pypdf.__version__,
            "textin": textin_fp,
        }
    )


def _classify(layouts: Sequence[PageLayout]) -> list[str]:
    """只判页型（P5 需要；不排版、不产出任何块）。分类失败不致命：整份记 unclassified。"""
    try:
        return [str(k) for k in page_kinds(layouts, multicolumn_first=False)]
    except Exception as exc:
        _LOG.warning("page classification failed", extra={"error": type(exc).__name__})
        return [KIND_UNCLASSIFIED] * len(layouts)


@dataclass(frozen=True)
class _Plan:
    layouts: tuple[PageLayout, ...]
    kinds: tuple[str, ...]
    l3_pages: tuple[int, ...]
    skips: Mapping[int, SkipRuleId]
    shadow: Mapping[int, tuple[SkipRuleId, ...]]

    @property
    def total(self) -> int:
        return len(self.layouts)


def _doc_kind(doc_type: str) -> DocKind:
    return "research_report" if doc_type == "research_report" else "announcement"


def _payload(blob: BlobStore, call: TextInCall) -> Mapping[str, object] | None:
    if call.json_ref is None:
        return None
    try:
        data = json.loads(blob.get(call.json_ref).decode("utf-8"))
    except (BlobNotFound, ValueError):
        return None
    return cast("Mapping[str, object]", data) if isinstance(data, dict) else None


def _stores(
    blob: BlobStore, calls: Sequence[TextInCall], warnings: list[str], *, strict: bool
) -> dict[int, PageStore]:
    """每个 TextIn 调用的 JSON → 原文页码 → PageStore（P3 用不展开合并格的文本）。
    同时核对页码基（T4 风险）：strict（分段调用）时不一致即抛 PageOffsetMismatch，整份退回；
    整份调用只能告警（再退回也是同一份响应）。"""
    out: dict[int, PageStore] = {}
    for call in calls:
        payload = _payload(blob, call)
        if payload is None:
            warnings.append(f"TextIn 段 {list(call.pages)} 没有可读的 JSON，P3 只能对空文本核对")
            continue
        problem = page_offset_problem(payload, len(call.pages))
        if problem is not None:
            if strict:
                raise PageOffsetMismatch(problem)
            warnings.append(f"页码基核对失败（整份调用，无法重试）：{problem}")
        result = payload.get("result") or {}
        detail = (result.get("detail") if isinstance(result, dict) else None) or []
        stores, _unpaged = split_detail_by_page(detail)
        for local, store in stores.items():
            if 1 <= local <= len(call.pages):
                out[call.pages[local - 1]] = store
    return out


def _no_output(call: TextInCall | None, store: PageStore | None, text_layer: str | None) -> bool:
    """L3 页没有 TextIn JSON，或输出为空而文字层非空（空白页本来就没有输出，不算）。"""
    if call is None or call.json_ref is None:
        return True
    empty = store is None or not store.unexpanded_text.strip()
    return empty and bool((text_layer or "").strip())


def _assemble(tp: TextInPages) -> list[NormalizedBlock]:
    """按原文页序合并各段块，ordinal 重排为 0..n-1；每段开头尚无标题的块沿用上一段末尾的 section_path，
    使跨段的 section_path 连续（TextIn 每个子文档各自从空路径起算）。"""
    out: list[NormalizedBlock] = []
    carried = ""
    pages_of_call = [set(c.pages) for c in tp.calls]
    for pages in pages_of_call:
        inherit = True
        for page in sorted(pages):
            for b in tp.blocks_by_page.get(page, ()):
                if b.section_path:
                    inherit = False
                path = carried if (inherit and not b.section_path) else b.section_path
                out.append(replace(b, ordinal=len(out), section_path=path))
                if path:
                    carried = path
    return out


class TieredParser:
    """实现 DocumentParser 与 DocTypeAwareParser。parse() 不知道文档类型，等价于 TextIn。"""

    def __init__(
        self,
        textin: DocumentParser,
        blob: BlobStore,
        *,
        doc_types: frozenset[str],
        enforced_skips: frozenset[SkipRuleId],
        textin_fp: str,
        extract: Callable[[bytes], list[PageLayout]] = extract_pages,
        classify: PageClassifier = _classify,
    ) -> None:
        periodic = sorted(doc_types & ALWAYS_L3_DOC_TYPES)
        if periodic:
            raise ValueError(f"{periodic} 永远整份走 L3，不能进入分页路由")
        self._textin, self._blob = textin, blob
        self._doc_types, self._enforced = doc_types, enforced_skips
        self._extract, self._classify = extract, classify
        self._fp = tiered_fingerprint(enforced=enforced_skips, textin_fp=textin_fp)

    @property
    def engine(self) -> str:
        return f"tiered:{TIERED_ALGORITHM_VERSION}+{self._fp}"

    def _sidecar_key(self, sha: str) -> str:
        return f"{SIDECAR_PREFIX}/{sha}/{self._fp}.json"

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        return self._textin.parse(file_bytes, owner_user=owner_user)

    def parse_for(
        self, file_bytes: bytes, *, doc_type: str, title: str | None, owner_user: str | None = None
    ) -> ParseResult:
        if owner_user is not None or doc_type not in self._doc_types:
            return self._textin.parse(file_bytes, owner_user=owner_user)
        if force_whole_document_l3(doc_type, title, None):
            return self._whole(
                file_bytes, reason=REASON_PERIODIC, kind=KIND_PERIODIC, forced=True, fallback=None
            )
        try:
            plan = self._plan(file_bytes, doc_type)
        except Exception as exc:
            reason = f"{FALLBACK_L0}：{type(exc).__name__}"
            _LOG.warning(
                "tiered parse fell back", extra={"stage": "plan", "error": type(exc).__name__}
            )
            return self._whole(
                file_bytes, reason=reason, kind=KIND_FALLBACK, forced=False, fallback=reason
            )
        tp = parse_pages_with_textin(
            self._textin, file_bytes, plan.l3_pages, total_pages=plan.total
        )
        try:
            return self._finish(file_bytes, plan, tp)
        except Exception as exc:
            reason = f"{FALLBACK_ASSEMBLY}：{type(exc).__name__}"
            _LOG.warning(
                "tiered parse fell back", extra={"stage": "assemble", "error": type(exc).__name__}
            )
            whole = self._whole(
                file_bytes, reason=reason, kind=KIND_FALLBACK, forced=False, fallback=reason
            )
            warnings = [*tp.warnings, f"分段结果被丢弃：{exc}", *whole.warnings]
            return replace(
                whole,
                warnings=warnings,
                textin_calls=tp.calls + whole.textin_calls,
                billable_pages=tp.billed_pages + (whole.billable_pages or 0),
            )

    def _plan(self, file_bytes: bytes, doc_type: str) -> _Plan:
        layouts = tuple(self._extract(file_bytes))
        if not layouts:
            raise ValueError("PDF 没有可提取的页")
        kinds = tuple(self._classify(layouts))
        if len(kinds) != len(layouts):
            raise ValueError("页型数与版面页数不一致")
        decisions = evaluate_skip_rules(
            [page_facts(x) for x in layouts], doc_kind=_doc_kind(doc_type)
        )
        skips = enforced_skips(decisions, self._enforced)
        shadow = {d.page: tuple(r for r in d.matched if r != skips.get(d.page)) for d in decisions}
        l3 = tuple(lay.page for lay in layouts if lay.page not in skips)
        return _Plan(layouts, kinds, l3, skips, shadow)

    def _routed(
        self, plan: _Plan, tp: TextInPages, stores: Mapping[int, PageStore]
    ) -> list[RoutedPage]:
        call_of = {p: c for c in tp.calls for p in c.pages}
        text = {lay.page: lay.text for lay in plan.layouts}
        out: list[RoutedPage] = []
        for page in range(1, plan.total + 1):
            kind = plan.kinds[page - 1]
            shadow = plan.shadow.get(page, ())
            if page in plan.skips:
                out.append(
                    RoutedPage(
                        page,
                        "skipped",
                        kind,
                        None,
                        skip_rule=plan.skips[page],
                        shadow_skip_rules=shadow,
                    )
                )
                continue
            call = call_of.get(page)
            if call is None:
                raise ValueError(f"第 {page} 页既未跳过也没有 TextIn 调用")
            store = stores.get(page)
            tables = _table_blocks(
                sum(1 for b in tp.blocks_by_page.get(page, ()) if b.block_type == "table"), store
            )
            reasons: tuple[str, ...] = (REASON_L0_OFF,)
            if _no_output(call, store, text.get(page)):
                reasons += (REASON_L3_NO_OUTPUT,)
            out.append(
                RoutedPage(
                    page,
                    "L3",
                    kind,
                    l3_page_input(
                        page,
                        engine=call.engine,
                        page_kind=kind,
                        reasons=reasons,
                        text_layer=text.get(page),
                        store=store,
                        table_blocks=tables,
                    ),
                    shadow_skip_rules=shadow,
                    textin_billed=call.billed_pages > 0,
                    textin_json_ref=call.json_ref,
                    image_ocr_numbers=image_ocr_numbers(store),
                )
            )
        return out

    def _finish(self, file_bytes: bytes, plan: _Plan, tp: TextInPages) -> ParseResult:
        sha = hashlib.sha256(file_bytes).hexdigest()
        warnings = list(tp.warnings)
        whole_doc = tp.whole_document is not None
        stores = _stores(self._blob, tp.calls, warnings, strict=not whole_doc)
        routed = self._routed(plan, tp, stores)
        if tp.whole_document is not None:
            whole = tp.whole_document
            ref = whole.json_ref or self._sidecar_key(sha)
            meta = DocumentMeta(True, whole.engine_version, True, page_count=plan.total)
            ledger = build_ledger(
                routed,
                routes_ref=ref,
                meta=meta,
                billed_pages=tp.billed_pages,
                fallback_reason=None,
            )
            if whole.json_ref is None:
                self._blob.put(
                    ref, json.dumps({"ledger": asdict(ledger)}, ensure_ascii=False).encode("utf-8")
                )
            return replace(
                whole,
                warnings=warnings,
                billable_pages=tp.billed_pages,
                textin_calls=tp.calls,
                ledger=ledger,
            )
        blocks = _assemble(tp)
        key = self._sidecar_key(sha)
        meta = DocumentMeta(
            True, self.engine, True, page_count=plan.total, skipped_pages=frozenset(plan.skips)
        )
        ledger = build_ledger(
            routed, routes_ref=key, meta=meta, billed_pages=tp.billed_pages, fallback_reason=None
        )
        md_key, markdown = self._write_sidecar(key, plan, tp, ledger, blocks)
        return ParseResult(
            blocks=blocks,
            page_count=plan.total,
            engine_version=self.engine,
            markdown=markdown,
            json_ref=key,
            md_ref=md_key,
            warnings=warnings,
            from_cache=tp.billed_pages == 0,
            billable_pages=tp.billed_pages,
            textin_calls=tp.calls,
            ledger=ledger,
        )

    def _write_sidecar(
        self,
        key: str,
        plan: _Plan,
        tp: TextInPages,
        ledger: ParseLedger,
        blocks: Sequence[NormalizedBlock],
    ) -> tuple[str, str]:
        text = {lay.page: lay.text for lay in plan.layouts}
        routes = [
            route_to_dict(
                PageRoute(
                    page=r.page,
                    tier=cast(Tier, r.tier),
                    engine=r.engine,
                    text_layer=bool(text[r.page].strip()),
                    reasons=r.reasons,
                    textin_billed=r.textin_billed,
                )
            )
            for r in ledger.pages
            if r.tier != "skipped"
        ]
        body = {
            "version": 1,
            "engine": self.engine,
            "routes": routes,  # tables/page_engines.py::routes_from_sidecar 读这个键（跳页不在其中）
            "skipped": [
                {"page": r.page, "rule": r.skip_rule} for r in ledger.pages if r.tier == "skipped"
            ],
            "shadow_skips": [
                {"page": r.page, "rules": list(r.shadow_skip_rules)}
                for r in ledger.pages
                if r.shadow_skip_rules
            ],
            "textin_calls": [call_to_dict(c) for c in tp.calls],
            "ledger": asdict(ledger),
        }
        self._blob.put(key, json.dumps(body, ensure_ascii=False).encode("utf-8"))
        markdown = "\n\n".join(
            f"<!-- p{r.page} {r.tier} -->\n"
            + "\n\n".join(b.content for b in blocks if b.page == r.page)
            for r in ledger.pages
        )
        md_key = key.removesuffix(".json") + ".md"
        self._blob.put(md_key, markdown.encode("utf-8"))
        return md_key, markdown

    def _whole(
        self, file_bytes: bytes, *, reason: str, kind: str, forced: bool, fallback: str | None
    ) -> ParseResult:
        r = self._textin.parse(file_bytes, owner_user=None)
        marked = [replace(b, source="textin") for b in r.blocks]
        warnings = list(r.warnings)
        try:
            layouts = self._extract(file_bytes)
            text = {lay.page: lay.text for lay in layouts}
        except Exception as exc:
            text = {}
            warnings.append(f"文字层提取失败：{type(exc).__name__}，P3 无法核对")
        total = r.page_count or len(text)
        if total < 1:
            return replace(r, blocks=marked, warnings=warnings)
        call = TextInCall(
            tuple(range(1, total + 1)),
            r.json_ref,
            r.md_ref,
            r.engine_version,
            0 if r.from_cache else total,
        )
        stores = _stores(self._blob, (call,), warnings, strict=False)
        tables = Counter(b.page for b in marked if b.block_type == "table")
        routed: list[RoutedPage] = []
        for p in range(1, total + 1):
            store = stores.get(p)
            reasons: tuple[str, ...] = (reason,)
            if _no_output(call, store, text.get(p)):
                reasons += (REASON_L3_NO_OUTPUT,)
            routed.append(
                RoutedPage(
                    p,
                    "L3",
                    kind,
                    l3_page_input(
                        p,
                        engine=r.engine_version,
                        page_kind=kind,
                        reasons=reasons,
                        text_layer=text.get(p),
                        store=store,
                        table_blocks=_table_blocks(tables.get(p, 0), store),
                    ),
                    textin_billed=call.billed_pages > 0,
                    textin_json_ref=r.json_ref,
                    image_ocr_numbers=image_ocr_numbers(store),
                )
            )
        ref = r.json_ref or self._sidecar_key(hashlib.sha256(file_bytes).hexdigest())
        meta = DocumentMeta(True, r.engine_version, True, page_count=total, forced_l3=forced)
        ledger = build_ledger(
            routed,
            routes_ref=ref,
            meta=meta,
            billed_pages=call.billed_pages,
            fallback_reason=fallback,
        )
        if r.json_ref is None:
            self._blob.put(
                ref, json.dumps({"ledger": asdict(ledger)}, ensure_ascii=False).encode("utf-8")
            )
        return replace(
            r,
            blocks=marked,
            warnings=warnings,
            billable_pages=call.billed_pages,
            textin_calls=(call,),
            ledger=ledger,
        )
