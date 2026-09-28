"""解析档位判定（公告双源接入计划 Task 4）。规则写死在这里，正反例各一条测试，
不靠人工检查 dry run 输出去发现分类错误。"""

from __future__ import annotations

import pytest

from ragdemo.ingest.parse_policy import ParseTier, parse_tier


@pytest.mark.parametrize(
    "title",
    [
        "海光信息技术股份有限公司2025年第三季度报告",
        "海光信息技术股份有限公司2025年第一季度报告",
        "海光信息技术股份有限公司2025年半年度报告摘要",
        "海光信息技术股份有限公司2025年年度报告摘要",
        "海光信息技术股份有限公司关于2025年半年度业绩预告的公告",
        "海光信息技术股份有限公司2025年度业绩快报",
    ],
)
def test_first_tier_titles(title: str) -> None:
    assert parse_tier(title) == ParseTier.FIRST


@pytest.mark.parametrize(
    "title",
    [
        "海光信息技术股份有限公司2025年年度报告",
        "海光信息技术股份有限公司2025年半年度报告",
    ],
)
def test_second_tier_titles(title: str) -> None:
    assert parse_tier(title) == ParseTier.SECOND


@pytest.mark.parametrize(
    "title",
    [
        "Hygon Information Technology 2025 Annual Report (English Version)",
        "海光信息技术股份有限公司2025年年度报告(英文版)",
        "关于取消2025年第一次临时股东会的提示性公告",
        "北京市中伦律师事务所关于海光信息技术股份有限公司股票激励计划授予预留限制性股票相关事项的法律意见书",
    ],
)
def test_no_parse_titles(title: str) -> None:
    assert parse_tier(title) == ParseTier.NONE


@pytest.mark.parametrize(
    ("title", "tier"),
    [
        ("海光信息技术股份有限公司关于2025年第三季度报告的更正公告", ParseTier.FIRST),
        ("海光信息技术股份有限公司2025年年度报告(修订版)", ParseTier.SECOND),
    ],
)
def test_corrections_keep_the_original_tier(title: str, tier: ParseTier) -> None:
    """更正 / 修订公告档位同原文——交给 DocumentWriter 既有的更正路由处理版本关系，
    这里只负责"这类文档值不值得花钱解析"，不重复判断是不是更正。"""
    assert parse_tier(title) == tier
