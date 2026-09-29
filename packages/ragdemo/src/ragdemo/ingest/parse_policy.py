"""解析档位判定（公告双源接入计划 Task 4）：一个纯函数，输入归一化后的标题，
输出这份公告值不值得花钱解析、解析到哪一批。

规则来自负责人 2026-09-28 的决定（D2）：先只对季报、年报/半年报摘要、业绩预告、
业绩快报解析（`ParseTier.FIRST`）；年报/半年报全文体量大，作为第二批
（`ParseTier.SECOND`）；其余公告只存 PDF、不解析（`ParseTier.NONE`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class ParseTier(StrEnum):
    FIRST = "first"  # 季报、年报/半年报摘要、业绩预告、业绩快报
    SECOND = "second"  # 年报/半年报全文
    NONE = "none"  # 其余：只存 PDF，不解析


# 一律不解析：英文版本（中文切块/表格描述规则对英文标题不成立）、已取消的事项。
_NEVER_PARSE = re.compile(r"英文版|English|取消.*(公告|事项)|提示性公告")

# 更正/修订公告：标题在原文档名后面又加了一段说明（"的更正公告"、"(修订版)"……），
# 先把这段剥掉，按被更正的原文档判定档位——更正一份年报全文，档位还是第二批，
# 不会因为多了"更正"两个字就被误判成不解析。剥不掉就用原标题继续判断。
_CORRECTION_SUFFIX = re.compile(r"(的)?(更正|修订)(公告|版|说明)?[)）]?$")

_FIRST_TIER = re.compile(r"第[一三]季度报告$|(半)?年度报告摘要$|业绩预告|业绩快报")
_SECOND_TIER = re.compile(r"(半)?年度报告$")


def parse_tier(title: str) -> ParseTier:
    if _NEVER_PARSE.search(title):
        return ParseTier.NONE
    base = _CORRECTION_SUFFIX.sub("", title).rstrip("(（")
    if _SECOND_TIER.search(base):
        return ParseTier.SECOND
    if _FIRST_TIER.search(base):
        return ParseTier.FIRST
    return ParseTier.NONE


@dataclass
class LayerReport:
    """一个公告全文层（巨潮/披露易/EDGAR，公告双源接入计划 Task 4/7/8）一次运行的结果。
    共享同一个形状：目录 + 选择性下载 + 按 `parse_tier` 分批解析的编排完全一样，
    只是取数适配器不同。"""

    listed: int = 0
    indexed: int = 0
    downloaded: int = 0
    parsed_docs: int = 0
    parsed_pages: int = 0
    stored_unparsed: int = 0
    skipped_budget: int = 0
    # 解析出了块，但 validate_chunks 判定不合格（最常见：content_desc 生成被
    # describe.py 的违禁词过滤拒绝）——落一行无块 document 留痕，不能让一份公告
    # 的校验失败炸穿整批（2026-09-29 对 688041.SH 真实公告实测发现）。
    validation_failed: int = 0
    # 目录里有、但没有可下载 PDF 附件的公告（照样入目录，只是拿不到原文）。
    no_attachment: int = 0
    # 上次已经下载并落了 document 的公告，这次不重复下载（往前回填会重叠窗口）。
    already_downloaded: int = 0
    # 来源自报的公告总数（用于对账）；来源不给总数时为 None。
    source_total: int | None = None
    estimated_pages: dict[ParseTier, int] = field(default_factory=dict)


# dry run 时没有真实 PDF 可读页数，按档位给经验估算（页数取该档位常见公告的量级）。
ESTIMATED_PAGES: dict[ParseTier, int] = {
    ParseTier.FIRST: 12,
    ParseTier.SECOND: 180,
    ParseTier.NONE: 5,
}


# EDGAR 申报没有中文标题可判——按 form_type 直接映射（公告双源接入计划 Task 8）。
_FORM_TIER: dict[str, ParseTier] = {
    "10-K": ParseTier.SECOND,  # 年报，篇幅大
    "20-F": ParseTier.SECOND,  # 外国私人发行人年报，等价于 10-K
    "10-Q": ParseTier.FIRST,
    "6-K": ParseTier.FIRST,  # 外国私人发行人临时报告，等价于 8-K/10-Q 量级
    "8-K": ParseTier.FIRST,
}


def parse_tier_for_form(form_type: str) -> ParseTier:
    return _FORM_TIER.get(form_type, ParseTier.NONE)
