from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragdemo.parse.local.columns import REASON_GUTTER, REASON_VERTICAL, detect_columns, page_columns
from ragdemo.parse.local.decor import strip_decor
from ragdemo.parse.local.layout import PageLayout, Word, extract_pages
from ragdemo.parse.local.samples import layout_from_fixture
from ragdemo.parse.local.tables import cluster_rows
from ragdemo.parse.local.typeset import body_font_size, typeset_document
from tests.parse.local.test_typeset import LEAD, W, line, page

FIXTURES = Path("tests/fixtures/layouts")
LOCAL_FIXTURES = Path("data/experiments/l0v2/fixtures")  # 研报夹具含研究文本，gitignored，仅本机
RAW_RESEARCH = Path("docs/rawdata/688041-hygon/research")
SIDEBAR_FIXTURES = [
    "r_20250424_minsheng_p1",
    "r_20240819_taipingyang_p1",
    "r_20260415_shanxi_p1",
    "r_20250807_kaiyuan_p1",
]
SIDEBAR_RAW = [
    "AP202504241661650341",
    "AP202408191639328385",
    "AP202604151821234936",
    "AP202508071723109728",
]
SINGLE_FIXTURES = ["a_556_p1", "a_560_p1", "a_563_p1"]


def _rows(p: PageLayout) -> list[list[Word]]:
    return cluster_rows([w for w in p.words if w.upright])


def test_a_sidebar_whose_lines_never_share_a_row_with_the_body_is_detected() -> None:
    body = [line("正文" * 15, 100 + i * LEAD, cjk=True) for i in range(20)]  # x 60–360
    # 侧栏行与正文行错开 7pt
    side = [line("评级维持买入", 107 + i * 2 * LEAD, x=420, cjk=True) for i in range(8)]
    p = page(*body, *side)
    v = detect_columns(_rows(p), W, 10.0)
    assert v.multicolumn and v.reason == REASON_GUTTER and v.gutter is not None
    assert v.gutter.left_rows >= 3 and v.gutter.right_rows >= 3
    (res,) = typeset_document([p])
    assert res.status == "layout_unsupported" and res.reason == REASON_GUTTER


def test_a_vertical_stack_of_isolated_single_characters_is_detected() -> None:
    chars = [line(ch, 100 + i * LEAD, x=20, cjk=True) for i, ch in enumerate("公司研究")]
    body = [line("正文" * 15, 100 + i * LEAD, cjk=True) for i in range(12)]
    v = detect_columns(_rows(page(*chars, *body)), W, 10.0)
    assert v.multicolumn and v.reason == REASON_VERTICAL


def test_single_column_pages_with_short_lines_titles_and_a_signature_are_not_multicolumn() -> None:
    rows = [
        line("海光信息技术股份有限公司", 60, size=18, x=180, cjk=True),
        *[line("正文" * 15, 100 + i * LEAD, cjk=True) for i in range(10)],
        line("短行", 100 + 10 * LEAD, cjk=True),
        line("海光信息技术股份有限公司董事会", 100 + 12 * LEAD, x=380, cjk=True),
        line("2024年1月3日", 100 + 13 * LEAD, x=440, cjk=True),
    ]
    assert not detect_columns(_rows(page(*rows)), W, 10.0).multicolumn


def _fixture(name: str) -> PageLayout:
    for d in (FIXTURES, LOCAL_FIXTURES):
        f = d / f"{name}.json"
        if f.exists():
            return layout_from_fixture(json.loads(f.read_text(encoding="utf-8")))
    pytest.skip("研报夹具不入库（data/experiments 被 gitignore），本机没有这份文件")


@pytest.mark.parametrize("name", SIDEBAR_FIXTURES)
def test_masked_real_research_first_pages_with_sidebars_are_multicolumn(name: str) -> None:
    lay = _fixture(name)
    assert page_columns(lay, body_font_size([lay])).multicolumn


@pytest.mark.parametrize("name", SINGLE_FIXTURES)
def test_masked_real_announcement_first_pages_are_single_column(name: str) -> None:
    lay = _fixture(name)
    assert not page_columns(lay, body_font_size([lay])).multicolumn


@pytest.mark.parametrize("ap", SIDEBAR_RAW)
def test_real_research_first_page_sidebar_is_layout_unsupported(ap: str) -> None:
    hits = sorted(RAW_RESEARCH.glob(f"*_{ap}.pdf"))
    if not hits:
        pytest.skip("原始研报不入库（docs/rawdata/.gitignore），本机没有这份文件")
    stripped = [s.layout for s in strip_decor(extract_pages(hits[0].read_bytes()))]
    assert typeset_document(stripped)[0].status == "layout_unsupported"
