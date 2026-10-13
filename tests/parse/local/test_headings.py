# tests/parse/local/test_headings.py
from __future__ import annotations

import pytest

from ragdemo.parse.local.headings import (
    Candidate,
    Numbering,
    assign_levels,
    cn_numeral,
    keep_consistent,
    numbering_of,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("第一节重要提示", Numbering("section", 1)),
        ("第十二章 总则", Numbering("chapter", 12)),
        ("一、本期业绩预告情况", Numbering("cn", 1)),
        ("（二）业绩预告情况", Numbering("cn_paren", 2)),
        ("(3)主要技术门槛", Numbering("arabic_paren", 3)),
        ("1.1 重大风险提示", Numbering("decimal2", 1, (1,))),
        ("2.3.4 说明", Numbering("decimal3", 4, (2, 3))),
        ("4、股东情况", Numbering("arabic", 4)),
        ("1．经财务部门初步测算", Numbering("arabic", 1)),
        ("重要内容提示：", Numbering("keyword", 0)),
        ("附件一：独立董事意见", Numbering("keyword", 0)),
    ],
)
def test_numbering_patterns(text: str, expected: Numbering) -> None:
    assert numbering_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "3.5亿元的增量来自新品",
        "12.5%的增速",
        "1.5倍",
        "2022年1月1日至2022年12月31日",
        "一是加大研发投入",
        "十分重要",
        "本公司董事会及全体董事保证",
        "风险提示要点",
    ],
)
def test_body_text_and_unit_bearing_numbers_are_not_numbering(text: str) -> None:
    assert numbering_of(text) is None


@pytest.mark.parametrize(
    ("s", "n"),
    [("一", 1), ("十", 10), ("十二", 12), ("二十", 20), ("二十三", 23), ("甲", None), ("", None)],
)
def test_chinese_numerals(s: str, n: int | None) -> None:
    assert cn_numeral(s) == n


def _c(i: int, system: str, ordinal: int, scope: tuple[int, ...] = ()) -> Candidate:
    return Candidate(i, Numbering(system, ordinal, scope))  # type: ignore[arg-type]


def test_nested_sequences_with_resets_are_kept() -> None:
    cands = [
        _c(0, "cn", 1),
        _c(1, "cn_paren", 1),
        _c(2, "cn_paren", 2),
        _c(3, "cn", 2),
        _c(4, "cn_paren", 1),
    ]
    assert keep_consistent(cands) == frozenset({0, 1, 2, 3, 4})


def test_out_of_sequence_and_singleton_systems_are_dropped() -> None:
    assert keep_consistent([_c(0, "cn_paren", 3)]) == frozenset()
    assert keep_consistent([_c(0, "cn", 1), _c(1, "cn", 3)]) == frozenset()  # 只剩一个 → 单例体系
    assert keep_consistent([_c(0, "decimal2", 5, (3,))]) == frozenset()


def test_decimal_scopes_restart_under_each_section_and_keywords_are_always_kept() -> None:
    cands = [
        _c(0, "keyword", 0),
        _c(1, "section", 1),
        _c(2, "decimal2", 1, (1,)),
        _c(3, "decimal2", 2, (1,)),
        _c(4, "section", 2),
        _c(5, "decimal2", 1, (2,)),
    ]
    assert keep_consistent(cands) == frozenset(range(6))


def test_levels_follow_the_fixed_precedence_of_the_systems_present() -> None:
    assert assign_levels(["cn_paren", "cn", "keyword"], base=1) == {
        "cn": 2,
        "cn_paren": 3,
        "keyword": 2,
    }
    assert assign_levels(["decimal2", "section"], base=0) == {
        "section": 1,
        "decimal2": 2,
        "keyword": 1,
    }
    assert (
        assign_levels(["cn", "cn_paren", "arabic", "decimal2", "decimal3", "arabic_paren"], base=2)[
            "arabic_paren"
        ]
        == 6
    )


@pytest.mark.parametrize(
    "text",
    [
        "1.1股",
        "1.1 美元每股",
        "1.2 美元每股",
        "2.3 港元",
        "1.5 万股",
        "3.2 亿股",
        "4.5 万吨",
        "2.5 吨",
        "1.2 次",
        "1.5 项",
        "2.4 点",
        "3.5 手",
        "1.8 bp",
        "2.5 户",
        "2.5 日元",
        "2.5 欧元",
    ],
)
def test_more_unit_bearing_numbers_are_not_numbering(text: str) -> None:
    assert numbering_of(text) is None


@pytest.mark.parametrize(
    ("text", "system"),
    [
        ("1.1 股东情况", "decimal2"),
        ("1.2 项目概况", "decimal2"),
        ("1.3 次要风险", "decimal2"),
        ("2.1 点评与展望", "decimal2"),
        ("4、股东情况", "arabic"),
        ("1.1 手机业务", "decimal2"),
        ("1.2 吨位结构", "decimal2"),
    ],
)
def test_heading_words_that_start_with_a_unit_character_stay_numbering(
    text: str, system: str
) -> None:
    n = numbering_of(text)
    assert n is not None and n.system == system
