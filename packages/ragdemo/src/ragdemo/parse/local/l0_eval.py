"""L0 评估的纯聚合函数（不读库、不读文件系统；脚本负责取数）。

【仅检测器诊断，非计划 G4/G5/G11 口径】多栏检测的评估口径：标注 × 检出的混淆表；
`unsure` 不进任何比率（只报个数）；
漏检率 = multi 页中未检出的占比，虚警率 = single 页中被检出的占比。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

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
