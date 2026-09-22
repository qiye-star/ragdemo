"""classify_tier：纯函数，不需要数据库。

分类顺序很关键：零块必须优先于 parse_engine 字符串判断——一份因为
`textin:skipped`/`blob:missing` 等永久失败标记而写入的文档，parse_engine
字符串长得像"textin:xxx"，但它没有任何块，必须被分到 failed 而不是 B/C。
"""

from __future__ import annotations

from ragdemo.api.queries.parse_ops import classify_tier


def test_vendor_prefix_is_tier_a() -> None:
    tier = classify_tier(
        parse_engine="vendor:mock-announcements", supersedes_doc_id=None, block_count=4
    )
    assert tier == "A"


def test_textin_prefix_without_supersedes_is_tier_b() -> None:
    tier = classify_tier(
        parse_engine="textin:3.29.3+abc123", supersedes_doc_id=None, block_count=100
    )
    assert tier == "B"


def test_textin_prefix_with_supersedes_is_tier_c() -> None:
    tier = classify_tier(parse_engine="textin:3.29.3+abc123", supersedes_doc_id=42, block_count=50)
    assert tier == "C"


def test_zero_blocks_is_failed_regardless_of_engine_string() -> None:
    """textin:skipped 长得像成功的 B 档标记，但零块才是判定失败的唯一依据
    （与 quality/checks.py::parse_success_rate_check 同一原则）。"""
    assert (
        classify_tier(parse_engine="textin:skipped", supersedes_doc_id=None, block_count=0)
        == "failed"
    )
    assert (
        classify_tier(parse_engine="blob:missing", supersedes_doc_id=None, block_count=0)
        == "failed"
    )
    assert (
        classify_tier(parse_engine="vendor:edgar", supersedes_doc_id=None, block_count=0)
        == "failed"
    )


def test_null_engine_with_blocks_is_other() -> None:
    assert classify_tier(parse_engine=None, supersedes_doc_id=None, block_count=5) == "other"


def test_unknown_prefix_is_other() -> None:
    assert (
        classify_tier(parse_engine="mineru:1.0", supersedes_doc_id=None, block_count=5) == "other"
    )
