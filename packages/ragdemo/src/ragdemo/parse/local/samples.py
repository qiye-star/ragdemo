# packages/ragdemo/src/ragdemo/parse/local/samples.py
"""L0 第二版的评估样本：按文档拆分调参集 / 留出集（固定种子），以及脱敏版面夹具。

拆分是防过拟合的根：调参集用来改规则，留出集只在最后跑一次。拆分清单入库，之后不得重生成。
研报原文不入 git（ADR-0014），测试用的真实版面只保留几何信息，文字逐字替换为占位符。"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from ragdemo.parse.local.layout import PageLayout, Word

SPLIT_SEED: Final = 20261012
Group = Literal["docs", "research"]
SampleSet = Literal["tune", "holdout"]


def require_tune_set(name: str) -> SampleSet:
    """调参类命令只许碰调参集：留出集只在 T6 的锁定运行器里跑一次。"""
    if name.startswith("holdout"):  # holdout / holdout2 一律拒绝
        raise ValueError("留出集只由 T6 的锁定运行器运行，调参类命令不得读取留出集（含 holdout2）")
    if name != "tune":
        raise ValueError(f"未知样本集：{name!r}")
    return "tune"


SPLIT_HEADER: Final = "group\tkey\tstratum\tset\tforced"

# v1 报告 §7 展示过、或被本计划用作测试夹具的文档：已经被看过，只能进调参集。
FORCED_TUNE: Final[frozenset[str]] = frozenset(
    {
        "552",
        "556",
        "560",
        "563",
        "547",
        "AP202504241661650341",  # 民生 2025-04-24：首页侧栏
        "AP202408191639328385",  # 太平洋 2024-08-19：首页竖排"公司研究"
        "AP202604151821234936",  # 山西 2026-04-15：首页行情侧栏
        "AP202508071723109728",  # 开源 2025-08-07：首页侧栏，v1 放宽阈值仍漏检
        "AP202503051644062530",  # 太平洋 2025-03-05：v1 唯一 closure_failed（图表碎片）
        "AP202505211676704193",  # 民生 2025-05-21：v1 §7 样例
        "AP202310201602162496",  # 开源 2023-10-20：v1 §7 样例
    }
)

_AP = re.compile(r"_(AP\d+)\.pdf$")


def research_key(filename: str) -> str:
    m = _AP.search(filename)
    if m is None:
        raise ValueError(f"研报文件名里没有 AP 编号：{filename}")
    return m.group(1)


def broker_of(filename: str) -> str:
    parts = filename.split("_")
    if len(parts) < 3:
        raise ValueError(f"研报文件名不是 日期_券商_标题_AP编号 格式：{filename}")
    return parts[1]


@dataclass(frozen=True)
class SampleDoc:
    group: Group
    key: str  # docs：doc_id；research：AP 编号
    stratum: str  # docs：doc_type；research：券商


@dataclass(frozen=True)
class SplitRow:
    doc: SampleDoc
    sample_set: SampleSet
    forced: bool = False


def split_samples(
    docs: Sequence[SampleDoc], *, forced_tune: frozenset[str], seed: int = SPLIT_SEED
) -> list[SplitRow]:
    """强制项进调参集；其余按（组, 分层）分桶，每桶 ⌊n/2⌋ 份进留出集，余下进调参集。"""
    rng = random.Random(seed)
    out: list[SplitRow] = []
    buckets: dict[tuple[str, str], list[SampleDoc]] = {}
    for d in sorted(docs, key=lambda d: (d.group, d.stratum, d.key)):
        if d.key in forced_tune:
            out.append(SplitRow(d, "tune", True))
        else:
            buckets.setdefault((d.group, d.stratum), []).append(d)
    for bucket in sorted(buckets):
        shuffled = list(buckets[bucket])
        rng.shuffle(shuffled)
        n_hold = len(shuffled) // 2
        out.extend(SplitRow(d, "holdout" if i < n_hold else "tune") for i, d in enumerate(shuffled))
    return sorted(out, key=lambda r: (r.doc.group, r.doc.key))


def write_split(rows: Sequence[SplitRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [SPLIT_HEADER]
    lines += [
        f"{r.doc.group}\t{r.doc.key}\t{r.doc.stratum}\t{r.sample_set}\t{int(r.forced)}"
        for r in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_split(path: Path) -> list[SplitRow]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != SPLIT_HEADER:
        raise ValueError(f"{path} 不是拆分清单（表头应为 {SPLIT_HEADER!r}）")
    rows: list[SplitRow] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        group, key, stratum, sample_set, forced = line.split("\t")
        if group not in ("docs", "research") or sample_set not in ("tune", "holdout"):
            raise ValueError(f"拆分清单行非法：{line!r}")
        rows.append(
            SplitRow(
                SampleDoc(cast(Group, group), key, stratum),
                cast(SampleSet, sample_set),
                forced == "1",
            )
        )
    return rows


def mask_text(text: str) -> str:
    """逐字替换（长度不变）：汉字 → 字，ASCII 字母 → x，数字 → 8；标点与空白原样保留。"""
    out: list[str] = []
    for ch in text:
        if "0" <= ch <= "9" or "０" <= ch <= "９":
            out.append("8")
        elif "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            out.append("字")
        elif ch.isascii() and ch.isalpha():
            out.append("x")
        else:
            out.append(ch)
    return "".join(out)


def layout_to_fixture(layout: PageLayout) -> dict[str, Any]:
    return {
        "page": layout.page,
        "width": round(layout.width, 2),
        "height": round(layout.height, 2),
        "text": mask_text(layout.text),
        "words": [
            [
                mask_text(w.text),
                round(w.x0, 2),
                round(w.x1, 2),
                round(w.top, 2),
                round(w.bottom, 2),
                w.upright,
                round(w.size, 2),
                w.bold,
            ]
            for w in layout.words
        ],
    }


def layout_from_fixture(d: Mapping[str, Any]) -> PageLayout:
    words = [
        Word(
            str(t),
            float(x0),
            float(x1),
            float(top),
            float(bottom),
            bool(up),
            float(size),
            bool(bold),
        )
        for t, x0, x1, top, bottom, up, size, bold in d["words"]
    ]
    return PageLayout(int(d["page"]), float(d["width"]), float(d["height"]), words, str(d["text"]))
