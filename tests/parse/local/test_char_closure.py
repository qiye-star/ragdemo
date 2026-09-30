from ragdemo.parse.local.char_closure import char_violations


def test_unit_swap_is_caught() -> None:
    assert char_violations("万元", "亿元") == ["万"]


def test_invented_chinese_numerals_are_caught() -> None:
    assert char_violations("增长三倍", "增长") == ["三", "倍"]


def test_markdown_punctuation_and_whitespace_are_ignored() -> None:
    assert char_violations("# 收入 | 91.2\n- **增长**，`x`", "收入91.2 增长 x") == []


def test_counts_are_multiset() -> None:
    assert char_violations("增增", "增") == ["增"]
