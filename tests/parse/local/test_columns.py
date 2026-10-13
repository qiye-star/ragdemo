from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragdemo.parse.local import columns
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


# ---- A1 / A2：竖排单字列 ----


def _body(n: int = 12, x: float = 60.0) -> list[list[Word]]:
    return [line("正文" * 15, 100 + i * LEAD, x=x, cjk=True) for i in range(n)]


def _is_vertical(*lines: list[Word], body: float = 10.0) -> bool:
    v = detect_columns(_rows(page(*lines)), W, body)
    return v.multicolumn and v.reason == REASON_VERTICAL


def test_a_stack_whose_x0_jitters_across_an_odd_integer_is_still_detected() -> None:
    xs = [20.9995, 21.0005, 20.9995, 21.0005]
    chars = [line(ch, 100 + i * LEAD, x=xs[i], cjk=True) for i, ch in enumerate("公司研究")]
    assert _is_vertical(*chars, *_body())


def test_a_latin_letter_stack_is_detected() -> None:
    chars = [line(ch, 100 + i * LEAD, x=20) for i, ch in enumerate("REPORT")]
    assert _is_vertical(*chars, *_body())


def test_a_digit_stack_is_detected() -> None:
    chars = [line(ch, 100 + i * LEAD, x=20) for i, ch in enumerate("2026")]
    assert _is_vertical(*chars, *_body())


def test_a_stack_1_6_em_from_the_body_is_detected_at_the_loose_end_of_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(columns, "VERTICAL_ISOLATION_EM", 1.0)
    # 单字宽 10：x0=34 -> x1=44，与正文（x=60）相距 16pt = 1.6 em
    chars = [line(ch, 100 + i * LEAD, x=34, cjk=True) for i, ch in enumerate("公司研究")]
    assert _is_vertical(*chars, *_body())


def test_a_stack_at_a_pitch_of_3_6_em_is_detected_at_the_loose_end_of_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(columns, "VERTICAL_PITCH_EM", 5.0)
    chars = [line(ch, 100 + i * 36, size=20, x=20, cjk=True) for i, ch in enumerate("公司研究")]
    assert _is_vertical(*chars, *_body(n=12))


def test_a_stack_pitch_beyond_the_limit_is_not_a_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(columns, "VERTICAL_PITCH_EM", 3.0)
    chars = [line(ch, 100 + i * 36, size=20, x=20, cjk=True) for i, ch in enumerate("公司研究")]
    assert not _is_vertical(*chars, *_body(n=12))  # 36pt = 3.6 em > 3.0


def test_a_stack_of_min_chars_minus_one_is_not_a_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(columns, "VERTICAL_MIN_CHARS", 3)
    chars = [line(ch, 100 + i * LEAD, x=20, cjk=True) for i, ch in enumerate("公司")]
    assert not detect_columns(_rows(page(*chars, *_body())), W, 10.0).multicolumn


def test_a_digit_stack_inside_a_table_is_not_a_vertical_stack() -> None:
    rows = [
        line("Item", 300 + i * LEAD, x=60)
        + line(str(i + 1), 300 + i * LEAD, x=260)
        + line("val", 300 + i * LEAD, x=420)
        for i in range(4)
    ]
    p = page(*_body(n=10), *rows)
    assert not page_columns(p, 10.0).multicolumn
    assert _is_vertical(*rows)  # 不剔除表格行就会误判：整页入口必须先剔除


def test_a_vertical_stack_is_handed_back_not_glued_onto_body_lines() -> None:
    chars = [line(ch, 100 + i * LEAD, x=20) for i, ch in enumerate("2026")]
    (res,) = typeset_document([page(*chars, *_body())])
    assert res.status == "layout_unsupported" and res.reason == REASON_VERTICAL


# ---- A3：栏间空白带必须两侧纵向重叠 ----


def _short(n: int, y0: float, x: float) -> list[list[Word]]:
    return [line("短行文字", y0 + i * LEAD, x=x, cjk=True) for i in range(n)]


def _two_columns_rows(n: int, start: int = 0) -> list[list[Word]]:
    return [
        line("左栏文字内容", 100 + (start + i) * LEAD, x=60, cjk=True)
        + line("右栏文字内容", 100 + (start + i) * LEAD, x=330, cjk=True)
        for i in range(n)
    ]


def test_a_signature_below_the_body_is_not_a_gutter() -> None:
    wide = [line("正文" * 40, 100 + i * LEAD, cjk=True) for i in range(10)]  # 60–460
    closing = _short(3, 100 + 10 * LEAD, 60)
    signature = _short(3, 100 + 13 * LEAD, 420)
    assert not detect_columns(_rows(page(*wide, *closing, *signature)), W, 10.0).multicolumn


def test_a_three_line_sidebar_beside_the_body_is_a_gutter() -> None:
    body = [line("正文" * 15, 100 + i * LEAD, cjk=True) for i in range(10)]  # 60–360
    wide = [line("正文" * 40, 240 + i * LEAD, cjk=True) for i in range(4)]
    side = _short(3, 100 + 2 * LEAD, 420)
    v = detect_columns(_rows(page(*body, *side, *wide)), W, 10.0)
    assert v.multicolumn and v.reason == REASON_GUTTER


def test_a_full_width_banner_inside_a_two_column_page_does_not_break_the_gutter() -> None:
    rows = [*_two_columns_rows(4), line("通栏标题" * 12, 100 + 4 * LEAD, cjk=True)]
    rows += _two_columns_rows(4, start=5)
    assert detect_columns(_rows(page(*rows)), W, 10.0).multicolumn


def test_a_page_of_mostly_banner_rows_is_not_flagged() -> None:
    # 重叠区间内：3 行两栏行被 9 行通栏行隔开（通栏占 75%）-> 不算栏间空白带
    rows: list[list[Word]] = []
    for k in range(3):
        rows += _two_columns_rows(1, start=4 * k)
        rows += [line("通栏正文" * 12, 100 + (4 * k + 1 + j) * LEAD, cjk=True) for j in range(3)]
    assert not detect_columns(_rows(page(*rows)), W, 10.0).multicolumn


def test_two_columns_with_offset_baselines_are_a_gutter() -> None:
    left = [line("左栏文字内容", 100 + i * 2 * LEAD, x=60, cjk=True) for i in range(6)]
    right = [line("右栏文字内容", 107 + i * 2 * LEAD, x=330, cjk=True) for i in range(6)]
    assert detect_columns(_rows(page(*left, *right)), W, 10.0).multicolumn


def test_key_value_rows_count_for_both_sides_so_three_of_them_are_flagged_and_two_are_not() -> None:
    # 行内两段（标签 + 值）的行同时算左右两侧各一行：3 行即成栏（保守：交给上层降级）
    def kv(n: int) -> list[list[Word]]:
        return [
            line("项目名称", 100 + i * LEAD, x=60, cjk=True)
            + line("项目金额", 100 + i * LEAD, x=330, cjk=True)
            for i in range(n)
        ]

    assert detect_columns(_rows(page(*kv(3))), W, 10.0).multicolumn
    assert not detect_columns(_rows(page(*kv(2))), W, 10.0).multicolumn


# ---- A4：边界 ----


def _two_col_gap(gap: float, rows: int = 6) -> list[list[Word]]:
    # 左栏右缘 260；右栏左缘 = 260 + gap
    return [
        line("正文" * 10, 100 + i * LEAD, x=60, cjk=True)
        + line("右栏文字", 100 + i * LEAD, x=260 + gap, cjk=True)
        for i in range(rows)
    ]


def test_the_gutter_width_boundary_is_inclusive_at_the_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(columns, "MIN_GUTTER_EM", 1.5)
    assert detect_columns(_rows(page(*_two_col_gap(15.0))), W, 10.0).multicolumn
    assert not detect_columns(_rows(page(*_two_col_gap(14.0))), W, 10.0).multicolumn


def test_two_side_rows_are_not_enough_three_are(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(columns, "MIN_SIDE_ROWS", 3)
    assert not detect_columns(_rows(page(*_two_col_gap(30.0, rows=2))), W, 10.0).multicolumn
    assert detect_columns(_rows(page(*_two_col_gap(30.0, rows=3))), W, 10.0).multicolumn


def test_a_gutter_whose_left_edge_is_at_the_zone_lower_bound_counts_one_outside_does_not() -> None:
    def cols(left_end: float) -> list[list[Word]]:
        n = round((left_end - 20) / 10)
        return [
            line("正" * n, 100 + i * LEAD, x=20, cjk=True)
            + line("右栏文字右栏文字", 100 + i * LEAD, x=left_end + 40, cjk=True)
            for i in range(6)
        ]

    lo = columns.GUTTER_ZONE[0] * W
    assert detect_columns(_rows(page(*cols(lo))), W, 10.0).multicolumn
    assert not detect_columns(_rows(page(*cols(lo - 10))), W, 10.0).multicolumn


def test_a_gutter_whose_right_edge_is_beyond_the_zone_upper_bound_is_not_a_gutter() -> None:
    hi = columns.GUTTER_ZONE[1] * W

    def cols(right_start: float) -> list[list[Word]]:
        return [
            line("正文" * 16, 100 + i * LEAD, x=60, cjk=True)
            + line("右栏文字", 100 + i * LEAD, x=right_start)
            for i in range(6)
        ]

    assert detect_columns(_rows(page(*cols(hi))), W, 10.0).multicolumn
    assert not detect_columns(_rows(page(*cols(hi + 10))), W, 10.0).multicolumn
