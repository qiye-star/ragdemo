"""前置探测的纯逻辑（解析分级路由计划 T1）：扫描页占比统计与"L2 是否推迟"的判定。

门槛事先写死在这里，不得在看到数据后调整：两个比例任一达到门槛就建 L2（双引擎一致），
否则推迟——扫描页直接走 L3（TextIn），L1 只做有文字层页的版面与表格。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

SCAN_PAGE_RATIO_MIN = 0.02  # 无可用文字层的页 / 总页数
SCAN_DOC_RATIO_MIN = 0.05  # 含 >= 1 个无可用文字层页的文档 / 总文档数


@dataclass(frozen=True)
class ScanSummary:
    docs: int
    pages: int
    no_text_pages: int
    cid_pages: int
    docs_with_no_text: int

    @property
    def page_ratio(self) -> float:
        return self.no_text_pages / self.pages if self.pages else 0.0

    @property
    def doc_ratio(self) -> float:
        return self.docs_with_no_text / self.docs if self.docs else 0.0


def summarize_scan(reasons_by_doc: Mapping[str, Sequence[str | None]]) -> ScanSummary:
    """reasons_by_doc：文档 → 逐页 `needs_handback` 的结果（None = 文字层可用）。"""
    pages = sum(len(r) for r in reasons_by_doc.values())
    bad = [x for r in reasons_by_doc.values() for x in r if x is not None]
    cid = sum(1 for x in bad if "cid" in x)
    docs_bad = sum(1 for r in reasons_by_doc.values() if any(x is not None for x in r))
    return ScanSummary(len(reasons_by_doc), pages, len(bad), cid, docs_bad)


def l2_decision(s: ScanSummary) -> Literal["build_l2", "defer_l2"]:
    if s.page_ratio >= SCAN_PAGE_RATIO_MIN or s.doc_ratio >= SCAN_DOC_RATIO_MIN:
        return "build_l2"
    return "defer_l2"
