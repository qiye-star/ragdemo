import pytest

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.tiered.census import (
    Census,
    classify_pages,
    force_whole_document_l3,
    is_periodic_report,
    summarize,
)

# ---- 定期报告识别（测试串手写，仿真实标题形态）-------------------------------------------------


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("某某科技股份有限公司2024年年度报告", "annual"),
        ("2024年年度报告", "annual"),
        ("某某科技股份有限公司2024年年度报告摘要", "annual"),  # 摘要保守算定期报告
        ("某某科技股份有限公司2025年半年度报告", "half_year"),
        ("2025年半年度报告摘要", "half_year"),
        ("2025年半年度财务报告", "half_year"),
        ("某某科技股份有限公司2025年中期报告", "half_year"),
        ("某某科技股份有限公司2024年第三季度报告", "quarterly"),
        ("2025年三季度报告", "quarterly"),
        ("2025年一季度报告", "quarterly"),
        ("某某科技2025年第一季度报告", "quarterly"),
        ("2023年年度财务报告", "annual"),
    ],
)
def test_periodic_titles_are_recognised(title: str, kind: str) -> None:
    assert is_periodic_report(title, None, None) == kind


@pytest.mark.parametrize(
    "title",
    [
        "某某科技股份有限公司关于2024年半年度业绩预告的自愿性披露公告",
        "某某科技股份有限公司2025年度业绩快报公告",
        "年报信息披露重大差错责任追究制度（2025年10月修订）",
        "独立董事年报工作制度（2025年10月修订）",
        "半年报董事会决议公告",
        "关于2024年年度报告更正的公告",
        "某某科技股份有限公司关于召开2025年半年度业绩说明会的公告",
        "某某科技股份有限公司2024年度内部控制评价报告",
        "某某科技股份有限公司2024年度审计报告",
        "某某科技股份有限公司2024年度环境、社会及公司治理（ESG）报告",
        "某某科技股份有限公司2024年度独立董事述职报告",
        "某某科技股份有限公司2024年度募集资金存放与使用情况的专项报告",
        "某某证券股份有限公司关于某某科技2024年半年度持续督导跟踪报告",
        "关于超短期融资券和中期票据获准注册的公告",
        "某某科技股份有限公司2025年中期权益分派实施公告",
        "半年度非经营性资金占用及其他关联资金往来情况汇总表",
        "某某科技股份有限公司2024年度提质增效重回报专项行动方案的半年度评估报告",
        "",
    ],
)
def test_non_reports_are_not_periodic(title: str) -> None:
    assert is_periodic_report(title, None, None) is None


def test_filename_is_used_when_title_is_missing_or_uninformative() -> None:
    fn = "2025-03-29_某某科技股份有限公司2024年年度报告_1222950880.pdf"
    assert is_periodic_report(None, fn, 230) == "annual"
    assert is_periodic_report("公告", fn, None) == "annual"
    assert is_periodic_report(None, None, 230) is None  # 页数本身不构成证据


def test_exclusion_applies_to_filename_too() -> None:
    assert is_periodic_report(None, "2025-03-29_关于2024年年度报告更正的公告_1.pdf", None) is None


def test_shorthand_hits_on_tiny_documents_are_rejected() -> None:
    assert is_periodic_report("2024年年报", None, 2) is None  # 简称 + 2 页：通知而非报告
    assert is_periodic_report("2024年年报", None, 150) == "annual"
    assert is_periodic_report("2024年年报", None, None) == "annual"
    # 全称不受页数限制（摘要可能只有 2 页）
    assert is_periodic_report("2024年年度报告摘要", None, 2) == "annual"


def test_half_year_is_not_mistaken_for_annual() -> None:
    assert is_periodic_report("2024年半年度报告", None, None) == "half_year"
    assert is_periodic_report("2024年半年报", None, None) == "half_year"


def test_force_whole_document_l3() -> None:
    assert force_whole_document_l3("annual_report", None, None)
    assert force_whole_document_l3("quarterly", "", "")
    assert force_whole_document_l3("announcement", "某某科技股份有限公司2024年年度报告", None)
    assert force_whole_document_l3("announcement", None, "2025年半年度报告.pdf")
    assert not force_whole_document_l3("announcement", "关于回购股份进展的公告", None)
    assert not force_whole_document_l3("research_report", "行业深度报告", None)
    assert not force_whole_document_l3("announcement", None, None)


# ---- 页型分类 --------------------------------------------------------------------------------

BODY = 10.0


def _w(text: str, x0: float, top: float, *, upright: bool = True) -> Word:
    return Word(text, x0, x0 + 6.0 * len(text), top, top + BODY, upright, BODY)


def _page(words: list[Word], text: str | None = None) -> PageLayout:
    t = text if text is not None else " ".join(w.text for w in words)
    return PageLayout(1, 595.0, 842.0, words, t)


def _prose_rows(n: int, start: float = 60.0) -> list[Word]:
    out: list[Word] = []
    for i in range(n):
        x = 60.0
        for tok in ("本公司主要从事处理器产品的研发设计", "销售业务并持续提升自主创新能力"):
            out.append(_w(tok, x, start + i * 14))
            x += 6.0 * len(tok) + 3
    return out


def _table_rows(n: int, start: float = 60.0) -> list[Word]:
    out: list[Word] = []
    for i in range(n):
        top = start + i * 14
        out += [
            _w("营业收入", 60, top),
            _w("1,234.56", 250, top),
            _w("2,345.67", 350, top),
            _w("3,456.78", 450, top),
        ]
    return out


def test_no_text_layer_page() -> None:
    page = _page([_w("1", 300, 800)])
    assert classify_pages([page]) == ["no_text_layer"]


def test_cid_text_is_no_text_layer() -> None:
    text = "(cid:12)" * 30
    assert classify_pages([_page(_prose_rows(3), text)]) == ["no_text_layer"]


def test_financial_statement_wins_over_table() -> None:
    words = [_w("合并资产负债表", 200, 40), *_table_rows(12, 80)]
    assert classify_pages([_page(words)]) == ["financial_statement"]


def test_coordinate_table_page() -> None:
    words = [*_prose_rows(6), *_table_rows(4, 200)]
    assert classify_pages([_page(words)]) == ["table"]


def test_plain_prose_page() -> None:
    assert classify_pages([_page(_prose_rows(30))]) == ["plain_text"]


def test_rotated_words_make_a_page_multicolumn_class() -> None:
    words = [*_prose_rows(30), _w("横", 20, 300, upright=False)]
    assert classify_pages([_page(words)]) == ["multicolumn"]


def test_table_is_checked_before_multicolumn() -> None:
    # 裁决 U-D3：表格先于多栏。两栏正文 + 一块表：标签是 table，不是 multicolumn。
    left = [_w("左栏正文内容", 60, 60 + i * 14) for i in range(12)]
    right = [_w("右栏正文内容", 330, 60 + i * 14) for i in range(12)]
    words = [*left, *right, *_table_rows(4, 300)]
    assert classify_pages([_page(words)]) == ["table"]


def test_two_column_page_without_table() -> None:
    left = [_w("左栏正文内容", 60, 60 + i * 14) for i in range(12)]
    right = [_w("右栏正文内容", 330, 60 + i * 14) for i in range(12)]
    assert classify_pages([_page([*left, *right])]) == ["multicolumn"]


# ---- 汇总 ------------------------------------------------------------------------------------


def _census() -> Census:
    return summarize(
        {
            "a": ("announcement", ["plain_text", "plain_text", "no_text_layer", "table"]),
            "b": ("announcement", ["financial_statement", "plain_text"]),
            "p": ("periodic", ["financial_statement"] * 3 + ["plain_text"]),
            "r": ("research", ["table", "plain_text", "multicolumn", "no_text_layer"]),
        }
    )


def test_summarize_counts_docs_and_pages_per_group() -> None:
    c = _census()
    assert c.docs == {"announcement": 2, "periodic": 1, "research": 1}
    assert c.page_count(("announcement",)) == 6
    assert c.page_count(("periodic",)) == 4
    assert c.kind_count("plain_text", ("announcement",)) == 3
    assert c.share("plain_text", ("announcement",)) == pytest.approx(0.5)
    assert c.share("financial_statement", ("periodic",)) == pytest.approx(0.75)


def test_routing_scope_excludes_periodic_reports() -> None:
    c = _census()
    assert c.page_count() == 10  # 6 + 4，不含定期报告
    assert c.scan_share() == pytest.approx(2 / 10)


def test_statement_share_is_over_pages_with_a_text_layer() -> None:
    c = _census()
    # 路由范围：有文字层页 = 10 - 2 = 8；报表页 1
    assert c.statement_share() == pytest.approx(1 / 8)
    assert c.statement_share(("periodic",)) == pytest.approx(3 / 4)


def test_empty_census_has_zero_shares() -> None:
    c = summarize({})
    assert c.scan_share() == 0.0 and c.statement_share() == 0.0 and c.share("table") == 0.0
