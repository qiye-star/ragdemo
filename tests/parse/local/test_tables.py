from ragdemo.parse.local.layout import Word
from ragdemo.parse.local.tables import (
    cluster_rows,
    column_edges,
    find_table_regions,
    grid_to_markdown,
    rows_to_grid,
)


def w(text: str, x0: float, x1: float, top: float) -> Word:
    return Word(text, x0, x1, top, top + 10)


TABLE = [
    w("项目", 10, 40, 100),
    w("2024年", 200, 240, 100),
    w("2023年", 300, 340, 100),
    w("营业收入", 10, 60, 120),
    w("91.2", 200, 220, 120),
    w("60.1", 300, 320, 120),
    w("净利润", 10, 50, 140),
    w("19.3", 200, 220, 141),
    w("12.7", 300, 320, 140),
]


def test_rows_are_clustered_by_y_with_tolerance() -> None:
    rows = cluster_rows(TABLE)
    assert [[x.text for x in r] for r in rows] == [
        ["项目", "2024年", "2023年"],
        ["营业收入", "91.2", "60.1"],
        ["净利润", "19.3", "12.7"],
    ]


def test_columns_split_at_gaps_and_grid_renders_markdown() -> None:
    rows = cluster_rows(TABLE)
    cols = column_edges(rows)
    assert len(cols) == 3
    md = grid_to_markdown(rows_to_grid(rows, cols))
    assert md.splitlines()[0] == "| 项目 | 2024年 | 2023年 |"
    assert "| 营业收入 | 91.2 | 60.1 |" in md


def test_a_wrapped_paragraph_has_no_table_region() -> None:
    para = [
        w("本公司主要从事", 10, 90, 100),
        w("高端处理器", 95, 150, 100),
        w("的研发设计与销售", 10, 100, 115),
        w("并提供相关服务", 105, 180, 115),
        w("报告期内业绩稳定", 10, 100, 130),
    ]
    assert find_table_regions(cluster_rows(para)) == []


def test_a_real_table_is_one_region() -> None:
    regions = find_table_regions(cluster_rows(TABLE))
    assert len(regions) == 1 and len(regions[0]) == 3


def test_a_sentence_above_a_table_does_not_corrupt_the_columns() -> None:
    # 混排页（正文 + 表）是最常见的形态：若整页一起投影算列，正文句子会把各列桥接成一列。
    sent = [
        ("Revenue", 60, 105),
        ("grew", 108, 130),
        ("steadily", 133, 172),
        ("during", 175, 205),
        ("the", 208, 224),
        ("reporting", 227, 270),
        ("period.", 273, 305),
    ]
    words = [w(t, a, b, 80) for t, a, b in sent]
    body = [("Item", "2024", "2023"), ("Revenue", "91.2", "60.1"), ("Net profit", "19.3", "12.7")]
    for i, (label, a, b) in enumerate(body):
        top = 140 + 20 * i
        words += [
            w(label, 60, 60 + 6 * len(label), top),
            w(a, 260, 260 + 6 * len(a), top),
            w(b, 360, 360 + 6 * len(b), top),
        ]
    (region,) = find_table_regions(cluster_rows(words))
    md = grid_to_markdown(rows_to_grid(region, column_edges(region)))
    assert "| Revenue | 91.2 | 60.1 |" in md and "grew" not in md


def test_two_tables_separated_by_a_text_row_are_two_regions() -> None:
    text_row = [w("以上为经营数据。", 10, 90, 180)]
    second = [Word(x.text, x.x0, x.x1, x.top + 100, x.bottom + 100) for x in TABLE]
    assert len(find_table_regions(cluster_rows(TABLE + text_row + second))) == 2
