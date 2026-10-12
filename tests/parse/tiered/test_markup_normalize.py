import json
import re
from pathlib import Path

from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.tiered.markup_normalize import normalize_markup
from ragdemo.parse.tiered.verify import _content_sequence, markup_violations

TAGS = re.compile(r"<[^>]*>")
RECORDED = Path("tests/fixtures/parse_engines/paddle_table_sample.json")


def test_real_paddle_markdown_passes_the_hardened_verifier_after_normalising() -> None:
    md = json.loads(RECORDED.read_text(encoding="utf-8"))["parse"]["markdown"]
    assert markup_violations(md) != []  # 原样是被拒的
    out = normalize_markup(md)
    assert markup_violations(out) == []
    # 属性里的数字（border="1"）不是内容，按去标签后的文本比较
    assert numbers_in(TAGS.sub(" ", out)) == numbers_in(TAGS.sub(" ", md))
    assert _content_sequence(TAGS.sub(" ", out)) == _content_sequence(TAGS.sub(" ", md))


def test_wrappers_are_unwrapped_and_presentational_attributes_dropped() -> None:
    md = (
        '<DIV style="text-align: center;"><HTML><BODY>'
        '<TABLE border="1" class="x"><TR align="left">'
        '<TD width="9" ROWSPAN="2" colspan=3 style="a>b">1.5</TD>'
        "<th height=3>x</th></TR></TABLE></BODY></HTML></DIV>"
    )
    assert normalize_markup(md) == (
        '<table><tr><td rowspan="2" colspan="3">1.5</td><th>x</th></tr></table>'
    )


def test_text_and_non_table_markup_are_left_alone() -> None:
    md = "收入 增长 5% <b>x</b> a < b 和 c > d\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert normalize_markup(md) == md


def test_idempotent() -> None:
    md = '<div><table border="1"><tr><td>1</td></tr></table></div>'
    once = normalize_markup(md)
    assert normalize_markup(once) == once


# ---- round 2: the normaliser must never alter text, only tag syntax ----
TAG_SYNTAX = re.compile(r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9]*(?=[\s/>])[^>]*>", re.DOTALL)

CORPUS = [
    "<div>91</div><div>.2</div>",
    "<div>2024</div><div>2023</div>",
    "<table><tr><td><div>1</div><div>2</div></td></tr></table>",
    "staff < head office > 3 people",
    "if a < div and b > c then 5",
    "<div-x>7</div-x> <html:div>8</html:div>",
    '<table><tr><td data-colspan="5">1</td><td title="colspan=7">2</td></tr></table>',
    "<!-- <div>9</div> --> 10",
    "<div and b> 11",
    '<DIV class="a"><P>12</P></DIV><body>13</body>',
    "a<div>1</div>b<div>2</div>c",
    "Revenue 91.2 (+5%) < 100 and > 50",
    json.loads(RECORDED.read_text(encoding="utf-8"))["parse"]["markdown"],
]


def _invariants(text: str) -> tuple[object, str]:
    stripped = TAG_SYNTAX.sub(" ", text)
    return numbers_in(stripped), _content_sequence(stripped)


def test_numbers_and_content_characters_survive_for_the_whole_corpus() -> None:
    for md in CORPUS:
        assert _invariants(normalize_markup(md)) == _invariants(md), md


def test_adjacent_wrappers_never_join_their_neighbours() -> None:
    out = normalize_markup("<div>91</div><div>.2</div>")
    assert "91.2" not in out and "91" in out and ".2" in out
    out = normalize_markup("<table><tr><td><div>1</div><div>2</div></td></tr></table>")
    assert "12" not in out.replace("<", " ").replace(">", " ").split()


def test_text_that_merely_looks_like_a_tag_is_untouched() -> None:
    for md in (
        "staff < head office > 3 people",
        "if a < div and b > c then 5",
        "<div-x>7</div-x>",
        "<html:div>8</html:div>",
        "<div and b> 11",
    ):
        assert normalize_markup(md) == md


def test_span_attributes_are_matched_by_exact_name() -> None:
    td = '<table><tr><td data-colspan="5" title="colspan=7" ROWSPAN=2>1</td></tr></table>'
    assert normalize_markup(td) == '<table><tr><td rowspan="2">1</td></tr></table>'
    odd = '<table><tr><td colspan="5>1</td></tr></table>'  # 无法切分：原样保留
    assert normalize_markup(odd) == odd
    bad = '<table><tr><td colspan="x">1</td></tr></table>'
    assert normalize_markup(bad) == bad


def test_comments_are_left_alone() -> None:
    md = '<!-- <div style="x">9</div><td colspan=3> --> 10'
    assert normalize_markup(md) == md


def test_w7_fullwidth_span_digits_and_non_html_whitespace_are_left_for_the_validator() -> None:
    fw = '<table><tr><td colspan="' + chr(0xFF12) + '">1</td></tr></table>'
    assert normalize_markup(fw) == fw
    nb = "<table><tr><td" + chr(0xA0) + 'colspan="2" border="1">1</td></tr></table>'
    assert normalize_markup(nb) == nb
