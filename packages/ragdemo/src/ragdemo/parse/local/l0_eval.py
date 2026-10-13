"""L0 评估的纯聚合函数（不读库、不读文件系统；脚本负责取数）。

【仅检测器诊断，非计划 G4/G5/G11 口径】多栏检测的评估口径：标注 × 检出的混淆表；
`unsure` 不进任何比率（只报个数）；
漏检率 = multi 页中未检出的占比，虚警率 = single 页中被检出的占比。"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal, TypeVar, cast

from ragdemo.parse.local.compare import HeadingMatchV2, table_data_rows
from ragdemo.parse.local.l0 import L0Page
from ragdemo.parse.textin import blocks_from_detail

LABEL_HEADER: Final = ("set", "group", "key", "page", "label", "note")


@dataclass(frozen=True)
class LabelRow:
    sample_set: str
    group: str
    key: str
    page: int
    label: str  # multi / single / unsure
    note: str = ""


@dataclass(frozen=True)
class EvalRow:
    group: str
    key: str
    page: int
    label: str
    detected: bool


@dataclass(frozen=True)
class Rates:
    multi: int
    single: int
    missed: tuple[tuple[str, int], ...]  # (key, page)
    false_alarms: tuple[tuple[str, int], ...]
    unsure: int

    @property
    def miss_rate(self) -> float:
        return len(self.missed) / self.multi if self.multi else 0.0

    @property
    def false_alarm_rate(self) -> float:
        return len(self.false_alarms) / self.single if self.single else 0.0


@dataclass(frozen=True)
class MultiColReport:
    confusion: Mapping[tuple[str, bool], int]  # (label, detected) -> 个数（含 unsure）
    overall: Rates
    by_group: Mapping[str, Rates]


def parse_labels(text: str) -> list[LabelRow]:
    """解析 multicol-labels.tsv（首行表头；note 列可缺省）。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or tuple(lines[0].split("\t")) != LABEL_HEADER:
        raise ValueError(f"标注表表头应为 {LABEL_HEADER}")
    out: list[LabelRow] = []
    for ln in lines[1:]:
        c = ln.split("\t")
        c += [""] * (len(LABEL_HEADER) - len(c))
        out.append(LabelRow(c[0], c[1], c[2], int(c[3]), c[4], c[5]))
    return out


def _rates(rows: Iterable[EvalRow]) -> Rates:
    multi = single = unsure = 0
    missed: list[tuple[str, int]] = []
    alarms: list[tuple[str, int]] = []
    for r in rows:
        if r.label == "multi":
            multi += 1
            if not r.detected:
                missed.append((r.key, r.page))
        elif r.label == "single":
            single += 1
            if r.detected:
                alarms.append((r.key, r.page))
        else:
            unsure += 1
    return Rates(multi, single, tuple(sorted(missed)), tuple(sorted(alarms)), unsure)


def aggregate_multicol(rows: Sequence[EvalRow]) -> MultiColReport:
    confusion: Counter[tuple[str, bool]] = Counter((r.label, r.detected) for r in rows)
    groups = sorted({r.group for r in rows})
    return MultiColReport(
        confusion=dict(confusion),
        overall=_rates(rows),
        by_group={g: _rates(r for r in rows if r.group == g) for g in groups},
    )


def format_report(rep: MultiColReport) -> str:
    """纯文本：混淆表、比率、分组、漏检页与虚警页（只含 key + page）。"""
    lines = ["label\tdetected\tcount"]
    for (label, detected), n in sorted(rep.confusion.items()):
        lines.append(f"{label}\t{int(detected)}\t{n}")

    def one(name: str, r: Rates) -> str:
        return (
            f"{name}\tmulti={r.multi}\tmiss={len(r.missed)} ({r.miss_rate:.4f})"
            f"\tsingle={r.single}\tfalse_alarm={len(r.false_alarms)} ({r.false_alarm_rate:.4f})"
            f"\tunsure_excluded={r.unsure}"
        )

    lines.append(one("ALL", rep.overall))
    lines.extend(one(g, r) for g, r in rep.by_group.items())
    lines.append("missed: " + " ".join(f"{k}:p{p}" for k, p in rep.overall.missed))
    lines.append("false_alarm: " + " ".join(f"{k}:p{p}" for k, p in rep.overall.false_alarms))
    return "\n".join(lines)


T = TypeVar("T")
C_TEXTIN_MEASURED: Final = 34.84 / 257  # TextIn 实测：25 次调用 257 页 34.84 元（分级路由计划背景）
Label = Literal["single", "multi", "unsure"]
PageKey = tuple[str, str, int]  # (组, 键, 页码)
CostKind = Literal[
    "l0_accepted",
    "table",
    "multicolumn",
    "chart_unresolved",
    "closure_failed",
    "financial_statement",
    "no_text_layer",
]
COST_KINDS: Final[tuple[CostKind, ...]] = (
    "l0_accepted",
    "table",
    "multicolumn",
    "chart_unresolved",
    "closure_failed",
    "financial_statement",
    "no_text_layer",
)


def textin_page_markdowns(payload: Mapping[str, Any]) -> dict[int, str]:
    """TextIn 缓存 → 逐页 markdown（标题 `#` × (outline_level + 1)；表格为 pipe 表；
    不含 content == 1 的页眉页脚；
    不含 figure 块——图表数据两条路径都不提取，参考里留着它只会把"按裁决不提取"算成漏召回）。"""
    detail = (payload.get("result") or {}).get("detail") or []
    pages: dict[int, list[str]] = {}
    for b in blocks_from_detail(detail, page_dims={}):
        if b.page is None or b.block_type == "figure":
            continue
        text = (
            "#" * ((b.level or 0) + 1) + " " + b.content if b.block_type == "title" else b.content
        )
        pages.setdefault(b.page, []).append(text)
    return {p: "\n\n".join(parts) for p, parts in pages.items()}


def textin_page_tables(payload: Mapping[str, Any]) -> dict[int, str]:
    """每页 TextIn 表格块的 markdown（G13b：表格数字是否被图表剥离带走）。"""
    detail = (payload.get("result") or {}).get("detail") or []
    pages: dict[int, list[str]] = {}
    for b in blocks_from_detail(detail, page_dims={}):
        if b.page is not None and b.block_type == "table":
            pages.setdefault(b.page, []).append(b.content)
    return {p: "\n\n".join(parts) for p, parts in pages.items()}


def content_audit(markdown: str, words: Sequence[str]) -> bool:
    """独立审计：输出的内容字符多重集合 == 文字层词的内容字符多重集合。"""
    return Counter(ch for ch in markdown if ch.isalnum()) == Counter(
        ch for w in words for ch in w if ch.isalnum()
    )


def page_has_table_rows(reference_md: str) -> bool:
    return bool(table_data_rows(reference_md))


@dataclass(frozen=True)
class MulticolRates:
    n_multi: int
    missed: int  # 标注 multi 却被 L0 接受
    n_single: int
    false_positive: int  # 标注 single 却被判 multicolumn

    @property
    def miss_rate(self) -> float:
        return self.missed / self.n_multi if self.n_multi else 1.0

    @property
    def fp_rate(self) -> float:
        return self.false_positive / self.n_single if self.n_single else 0.0


def multicolumn_rates(
    labels: Mapping[PageKey, Label], kinds: Mapping[PageKey, str], accepted: Mapping[PageKey, bool]
) -> MulticolRates:
    n_multi = missed = n_single = fp = 0
    for key, label in labels.items():
        if key not in kinds:
            raise ValueError(f"标注页 {key} 没有运行结果：先跑 run，再算门槛")
        if label == "multi":
            n_multi += 1
            missed += int(accepted[key])
        elif label == "single":
            n_single += 1
            fp += int(kinds[key] == "multicolumn")
    return MulticolRates(n_multi, missed, n_single, fp)


@dataclass(frozen=True)
class HeadingTotals:
    matched: int = 0
    cand_total: int = 0
    ref_total: int = 0
    rel_agree: int = 0
    rel_total: int = 0

    def add(self, m: HeadingMatchV2) -> HeadingTotals:
        return HeadingTotals(
            self.matched + m.matched,
            self.cand_total + m.cand_total,
            self.ref_total + m.ref_total,
            self.rel_agree + m.rel_agree,
            self.rel_total + m.rel_total,
        )

    @property
    def recall(self) -> float:
        return self.matched / self.ref_total if self.ref_total else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.cand_total if self.cand_total else 1.0

    @property
    def relative_level_agreement(self) -> float:
        return self.rel_agree / self.rel_total if self.rel_total else 1.0


def cost_kind(p: L0Page) -> CostKind:
    if p.accepted:
        return "l0_accepted"
    if p.kind == "plain_text":
        return "closure_failed"
    return p.kind


@dataclass(frozen=True)
class Prices:
    c_textin: float
    c_local: float = 0.0  # 自托管 L1 引擎每页摊销（未给定按 0，见分级路由计划开放问题 3）
    l1_enabled: bool = False
    l1_pass: float = 0.0  # L1 通过率：只能取分级路由计划 T7 的实测值

    def downstream(self) -> float:
        """非 L0、非报表、非扫描页的每页期望成本（L1 失败再 L3，两笔都付）。"""
        if self.l1_enabled:
            return self.c_local + (1 - self.l1_pass) * self.c_textin
        return self.c_textin


def cost_breakdown(counts: Mapping[CostKind, int], p: Prices) -> dict[CostKind, float]:
    """每类页对"每页均摊成本"的贡献。L0 在开发机 CPU 上 0.1–0.2 s / 页，计 0 元；
    报表页与无文字层页按 L3 计。"""
    total = sum(counts.get(k, 0) for k in COST_KINDS)
    if total == 0:
        raise ValueError("没有页，无法计算每页成本")
    unit: dict[CostKind, float] = {
        "l0_accepted": 0.0,
        "financial_statement": p.c_textin,
        "no_text_layer": p.c_textin,
    }
    return {k: counts.get(k, 0) * unit.get(k, p.downstream()) / total for k in COST_KINDS}


def blended_cost_per_page(counts: Mapping[CostKind, int], p: Prices) -> float:
    return sum(cost_breakdown(counts, p).values())


def routing_cost_inputs(counts: Mapping[CostKind, int]) -> tuple[float, float, float]:
    """换算成分级路由计划 T7 CostInputs 的 (scan_share, statement_share, l0_pass)。"""
    total = sum(counts.get(k, 0) for k in COST_KINDS)
    scan, stmt = counts.get("no_text_layer", 0), counts.get("financial_statement", 0)
    text = total - scan
    non_stmt = text - stmt
    return (
        scan / total if total else 0.0,
        stmt / text if text else 0.0,
        counts.get("l0_accepted", 0) / non_stmt if non_stmt else 0.0,
    )


GATES: Final[Mapping[str, float]] = {
    "G1": 0,
    "G2": 0,
    "G3": 0.99,
    "G3b": 0,
    "G4": 0.05,
    "G4_min_multi": 30,
    "G5": 0.15,
    "G6a": 0.60,
    "G6b": 0.85,
    "G6c": 0.85,
    "G7": 0.85,
    "G7_min_available": 30,
    "G7_min_judged": 60,
    "G8": 0.05,
    "G9": 0,
    "G9_min_pages": 30,
    "G10a": 0.30,
    "G10b": 0.15,
    "G11": 0.10,
    "G12": 1.0,
    "G13": 0.99,
    "G13_min_pages": 10,
    "G13b": 0.01,
    "G14a": 0.80,
    "G14a_min_charts": 30,
    "G14b": 0,
    "G14c": 0.70,
    "G14c_min_boxes": 20,
}
# U-D4：holdout2 上只有三条溯源硬线阻断，其余只报告
HOLDOUT2_BLOCKING: Final[tuple[str, ...]] = ("G1", "G2", "G3b")


@dataclass(frozen=True)
class FigureLabelTotals:
    pages: int = 0
    charts_total: int = 0
    charts_covered: int = 0
    boxes: int = 0
    boxes_chart: int = 0  # 标 chart 或 partial
    boxes_table_or_body: int = 0

    @property
    def recall(self) -> float:
        return self.charts_covered / self.charts_total if self.charts_total else 1.0

    @property
    def precision(self) -> float:
        return self.boxes_chart / self.boxes if self.boxes else 1.0


@dataclass(frozen=True)
class Gate:
    gid: str
    name: str
    value: float | None
    threshold: float
    op: str
    passed: bool


@dataclass(frozen=True)
class L0Metrics:
    closure_failed: int
    audit_inconsistent: int
    num_recall: float
    unexplained_missing: int
    multicol: MulticolRates | None
    heading: HeadingTotals
    research_heading_available: int
    research_heading_judged: int
    research_heading_precision: float | None
    leakage_rate: float
    spot_available: int
    spot_pages: int
    spot_order_failures: int | None
    coverage_announcement: float
    coverage_research: float
    multicol_share_announcement: float
    seconds_p95: float
    figure_pages_announcement: int  # 有图表区域的 announcement L0 接受页数（G13 适用性）
    figure_page_recall: float | None  # G13；页数不足时为 None
    figure_table_number_loss: float  # G13b
    figure_labels: FigureLabelTotals | None  # G14（U5）；未标注为 None


def _ge(gid: str, name: str, v: float | None) -> Gate:
    t = GATES[gid]
    return Gate(gid, name, v, t, ">=", v is not None and v >= t)


def _le(gid: str, name: str, v: float | None) -> Gate:
    t = GATES[gid]
    return Gate(gid, name, v, t, "<=", v is not None and v <= t)


def evaluate_gates(m: L0Metrics, *, final: bool) -> list[Gate]:
    mc = m.multicol
    enough_multi = mc is not None and mc.n_multi >= GATES["G4_min_multi"]
    gates = [
        _le("G1", "纯文字页 closure_failed 页数", float(m.closure_failed)),
        _le("G2", "独立审计不一致页数", float(m.audit_inconsistent)),
        _ge("G3", "L0 页数字召回（公告，micro）", m.num_recall),
        _le("G3b", "无法解释的缺失数字个数", float(m.unexplained_missing)),
        _le("G4", "多栏漏检率", mc.miss_rate if (mc is not None and enough_multi) else None),
        _le("G5", "多栏误检率", mc.fp_rate if mc is not None else None),
        _ge("G6a", "标题召回（公告）", m.heading.recall),
        _ge("G6b", "标题精确率（公告）", m.heading.precision),
        _ge("G6c", "标题相对层级一致率（公告）", m.heading.relative_level_agreement),
    ]
    if not final:
        gates.append(Gate("G7", "研报标题人工精确率（调参集不评）", None, GATES["G7"], ">=", True))
    elif m.research_heading_available < GATES["G7_min_available"]:
        gates.append(
            Gate(
                "G7", "研报标题人工精确率（标题不足 30 个，不适用）", None, GATES["G7"], ">=", True
            )
        )
    elif m.research_heading_judged < min(GATES["G7_min_judged"], m.research_heading_available):
        gates.append(Gate("G7", "研报标题人工精确率（判定数不足）", None, GATES["G7"], ">=", False))
    else:
        gates.append(_ge("G7", "研报标题人工精确率", m.research_heading_precision))
    gates.append(_le("G8", "表格泄漏页占比（公告）", m.leakage_rate))
    if not final:
        gates.append(Gate("G9", "阅读顺序人工抽查（调参集不评）", None, GATES["G9"], "<=", True))
    elif m.spot_order_failures is None or m.spot_pages < min(
        GATES["G9_min_pages"], m.spot_available
    ):
        gates.append(Gate("G9", "阅读顺序人工抽查（抽查页数不足）", None, GATES["G9"], "<=", False))
    else:
        gates.append(_le("G9", "阅读顺序错误页数", float(m.spot_order_failures)))
    gates += [
        _ge("G10a", "L0 覆盖率（公告）", m.coverage_announcement),
        _ge("G10b", "L0 覆盖率（研报）", m.coverage_research),
        _le("G11", "多栏页占比（公告）", m.multicol_share_announcement),
        _le("G12", "每页耗时 p95（秒）", m.seconds_p95),
    ]
    if m.figure_pages_announcement < GATES["G13_min_pages"]:
        gates.append(
            Gate("G13", "图表页数字召回（页数不足 10，不适用）", None, GATES["G13"], ">=", True)
        )
    else:
        gates.append(_ge("G13", "图表页数字召回（公告）", m.figure_page_recall))
    gates.append(_le("G13b", "被图表剥离带走的表格数字占比", m.figure_table_number_loss))
    fl = m.figure_labels
    if fl is None:
        gates.append(
            Gate("G14", "图表检出人工标注（未标注）", None, GATES["G14b"], "<=", not final)
        )
        return gates
    if fl.charts_total < GATES["G14a_min_charts"]:
        gates.append(
            Gate("G14a", "图表检出召回（图表不足 30，不适用）", None, GATES["G14a"], ">=", True)
        )
    else:
        gates.append(_ge("G14a", "图表检出召回", fl.recall))
    gates.append(_le("G14b", "框住表格或正文的检出框数", float(fl.boxes_table_or_body)))
    if fl.boxes < GATES["G14c_min_boxes"]:
        gates.append(
            Gate("G14c", "检出框精确率（框不足 20，不适用）", None, GATES["G14c"], ">=", True)
        )
    else:
        gates.append(_ge("G14c", "检出框精确率", fl.precision))
    return gates


def is_go(gates: Sequence[Gate]) -> bool:
    return all(g.passed for g in gates)


def is_go_holdout2(gates: Sequence[Gate]) -> bool:
    """U-D4：holdout2 只有溯源硬线（G1 / G2 / G3b）阻断，其余仅作信息。"""
    return all(g.passed for g in gates if g.gid in HOLDOUT2_BLOCKING)


def claim_holdout(lock: Path, *, git_sha: str, today: date) -> None:
    if lock.exists():
        raise SystemExit(
            f"留出集已经跑过（{lock.read_text(encoding='utf-8').strip()}）：本计划规定留出集只跑一次"
        )
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{today.isoformat()} {git_sha}\n", encoding="utf-8")


def _rows(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    return [
        dict(zip(head, (ln.split("\t") + [""] * len(head))[: len(head)], strict=True))
        for ln in lines[1:]
        if ln.strip()
    ]


def read_multicol_labels(path: Path, sample_set: str) -> dict[PageKey, Label]:
    out: dict[PageKey, Label] = {}
    for i, r in enumerate(_rows(path), start=2):
        if r["set"] != sample_set:
            continue
        if r["label"] not in ("single", "multi", "unsure"):
            raise ValueError(f"{path} 第 {i} 行未标注或标签非法：{r['label']!r}")
        if r["label"] != "unsure":
            out[(r["group"], r["key"], int(r["page"]))] = cast(Label, r["label"])
    return out


def read_judgments(path: Path) -> tuple[int, int]:
    """返回 (yes, no)；unsure 与空白不计。"""
    rows = _rows(path)
    return sum(r["judgment"] == "yes" for r in rows), sum(r["judgment"] == "no" for r in rows)


def read_spotcheck(path: Path) -> tuple[int, int]:
    """返回 (已判定页数, order_ok = no 的页数)。"""
    rows = [r for r in _rows(path) if r["order_ok"] in ("yes", "no")]
    return len(rows), sum(r["order_ok"] == "no" for r in rows)


def read_figure_labels(path: Path, sample_set: str) -> FigureLabelTotals:
    """U5 标注表 → 合计。note 为 unsure 的页不计；已抽中却未填 charts_total 的行报错；
    框数与标签数不符报错。"""
    t = FigureLabelTotals()
    for i, r in enumerate(_rows(path), start=2):
        if r["set"] != sample_set or r["note"].strip() == "unsure":
            continue
        if r["charts_total"] == "" or r["charts_covered"] == "":
            raise ValueError(f"{path} 第 {i} 行未标注")
        labels = [x.strip() for x in r["box_labels"].split(",") if x.strip()]
        if len(labels) != int(r["boxes"]) or any(
            x not in ("chart", "partial", "table", "body", "other") for x in labels
        ):
            raise ValueError(f"{path} 第 {i} 行框数 {r['boxes']} 与标签 {labels} 不符或标签非法")
        t = FigureLabelTotals(
            t.pages + 1,
            t.charts_total + int(r["charts_total"]),
            t.charts_covered + int(r["charts_covered"]),
            t.boxes + len(labels),
            t.boxes_chart + sum(x in ("chart", "partial") for x in labels),
            t.boxes_table_or_body + sum(x in ("table", "body") for x in labels),
        )
    return t


def sample_items(items: Sequence[T], n: int, seed: int) -> list[T]:
    if n >= len(items):
        return list(items)
    return random.Random(seed).sample(list(items), n)
