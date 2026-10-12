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
