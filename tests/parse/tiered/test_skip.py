import pytest

from ragdemo.parse.local.layout import Graphic, PageLayout
from ragdemo.parse.tiered.skip import (
    SKIP_RULES_VERSION,
    PageFacts,
    enforced_skips,
    evaluate_skip_rules,
    non_date_numbers,
    page_facts,
)
from ragdemo.parse.tiered.types import SKIP_RULE_IDS, parse_skip_rules

# 超过 200 个非空白字符：即使落在末页、含"董事会"，也不是签章页
BODY = "公司第三届董事会第五次会议审议通过了关于调整募集资金投资项目的议案，表决结果为同意。" * 6
SIGN = (
    "公司全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏。\n"
    "特此公告。\n海光信息技术股份有限公司董事会\n2025年10月13日"
)
DISCLAIMER = "免责声明：本报告仅供内部研究参考，不构成任何意见，读者应独立判断。" * 3


def _p(page: int, text: str, *, images: int = 0, curves: int = 0) -> PageFacts:
    return PageFacts(page, text, images, curves)


def _matched(pages: list[PageFacts], kind: str) -> dict[int, tuple[str, ...]]:
    return {d.page: d.matched for d in evaluate_skip_rules(pages, doc_kind=kind)}  # type: ignore[arg-type]


def test_page_facts_counts_embedded_images_and_curves_only() -> None:
    graphics = (
        Graphic("image", 0, 10, 0, 10),
        Graphic("curve", 0, 10, 0, 10, points=5),
        Graphic("rect", 0, 10, 0, 10),
        Graphic("line", 0, 10, 0, 1),
    )
    f = page_facts(PageLayout(3, 600.0, 800.0, [], "abc", graphics=graphics))
    assert (f.page, f.text, f.image_count, f.curve_count) == (3, "abc", 1, 1)


def test_dates_are_not_counted_but_other_numbers_are() -> None:
    assert non_date_numbers("2025年10月13日") == 0
    assert non_date_numbers("2025 年 10 月 13 日") == 0
    assert non_date_numbers("出席董事 9 名，2025年10月13日") == 1


def test_blank_page_needs_few_chars_no_image_no_curve_and_no_numbers() -> None:
    pages = [
        _p(1, BODY),
        _p(2, "  "),
        _p(3, "- 3 -"),
        _p(4, "", images=1),
        _p(5, "", curves=2),
        _p(6, BODY),
    ]
    m = _matched(pages, "announcement")
    assert m[2] == ("blank_page",)
    assert m[1] == m[3] == m[4] == m[5] == m[6] == ()


def test_signature_tail_is_only_the_short_last_page_without_numbers() -> None:
    assert _matched([_p(1, BODY), _p(2, SIGN)], "announcement")[2] == ("signature_tail",)
    assert _matched([_p(1, BODY), _p(2, SIGN + "\n同意 9 票")], "announcement")[2] == ()
    assert _matched([_p(1, SIGN), _p(2, BODY)], "announcement")[1] == ()
    assert _matched([_p(1, SIGN)], "announcement")[1] == ()
    assert _matched([_p(1, BODY), _p(2, SIGN)], "research_report")[2] == ()


def test_research_disclaimer_tail_only_in_the_last_three_pages_and_without_numbers() -> None:
    pages = [*(_p(i, BODY) for i in range(1, 5)), _p(5, DISCLAIMER)]
    assert _matched(pages, "research_report")[5] == ("research_disclaimer_tail",)
    rated = [*pages[:-1], _p(5, DISCLAIMER + "相对基准涨幅 15% 以上")]
    assert _matched(rated, "research_report")[5] == ()
    early = [_p(1, DISCLAIMER), *(_p(i, BODY) for i in range(2, 6))]
    assert _matched(early, "research_report")[1] == ()
    assert _matched(pages, "announcement")[5] == ()


def test_only_enforced_rules_skip_and_a_fully_skipped_document_skips_nothing() -> None:
    d = evaluate_skip_rules([_p(1, BODY), _p(2, "  "), _p(3, SIGN)], doc_kind="announcement")
    assert enforced_skips(d, frozenset()) == {}
    assert enforced_skips(d, frozenset({"blank_page"})) == {2: "blank_page"}
    assert enforced_skips(d, frozenset({"blank_page", "signature_tail"})) == {
        2: "blank_page",
        3: "signature_tail",
    }
    all_blank = evaluate_skip_rules([_p(1, " "), _p(2, "")], doc_kind="announcement")
    assert enforced_skips(all_blank, frozenset({"blank_page"})) == {}


def test_rule_names_are_validated_and_versioned() -> None:
    assert parse_skip_rules(" blank_page , signature_tail ") == frozenset(
        {"blank_page", "signature_tail"}
    )
    assert parse_skip_rules("") == frozenset()
    with pytest.raises(ValueError, match="未知的减页规则"):
        parse_skip_rules("blank_page,cover_page")
    assert SKIP_RULE_IDS == ("blank_page", "signature_tail", "research_disclaimer_tail")
    assert SKIP_RULES_VERSION == "skip-v1"
