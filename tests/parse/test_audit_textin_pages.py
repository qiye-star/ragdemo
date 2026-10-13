from __future__ import annotations

from collections import Counter

from ragdemo.parse.textin_audit import (
    CAT_ADJACENT_PAGE,
    CAT_FORMAT_VARIANT,
    CAT_IMAGE_OCR,
    CAT_MERGE_EXPANSION,
    CAT_SPACING,
    CAT_VALUE_ABSENT,
    classify_invented,
    examples,
    excess,
    heal_spacing,
    split_detail_by_page,
    split_lost,
    value_key,
)


def _cell(r: int, c: int, text: str, page: int = 1, rs: int = 1, cs: int = 1) -> dict[str, object]:
    return {
        "row": r,
        "col": c,
        "text": text,
        "page_id": page,
        "row_span": rs,
        "col_span": cs,
        "position": [10, 10, 50, 10, 50, 30, 10, 30],
    }


def test_split_detail_separates_header_footer_and_pages() -> None:
    detail = [
        {"type": "paragraph", "page_id": 1, "text": "正文 12", "content": 0},
        {"type": "paragraph", "page_id": 1, "text": "第 1 页", "content": 1, "sub_type": "footer"},
        {"type": "paragraph", "page_id": 2, "text": "次页 7", "content": 0},
    ]
    pages, unpaged = split_detail_by_page(detail)
    assert unpaged == 0
    assert pages[1].stored_text == "正文 12"
    assert pages[1].header_footer_text == "第 1 页"
    assert pages[2].stored_text == "次页 7"


def test_zero_based_page_ids_are_shifted() -> None:
    pages, _ = split_detail_by_page([{"type": "paragraph", "page_id": 0, "text": "x 1"}])
    assert list(pages) == [1]


def test_table_cells_go_to_their_own_page_and_merged_cells_expand() -> None:
    table = {
        "type": "table",
        "page_id": 1,
        "text": "<table/>",
        "cells": [
            _cell(0, 0, "合计", 1, rs=2),
            _cell(0, 1, "100", 1),
            _cell(1, 1, "200", 2),
        ],
    }
    pages, _ = split_detail_by_page([table])
    assert pages[1].stored == ["合计", "合计", "100"]
    assert pages[1].unexpanded == ["合计", "100"]
    assert pages[2].stored == ["200"]
    assert pages[1].table_boxes and pages[2].table_boxes


def test_image_items_recorded_as_ocr_text() -> None:
    pages, _ = split_detail_by_page(
        [{"type": "image", "page_id": 1, "text": "30% 26.9%", "sub_type": "chart"}]
    )
    assert pages[1].image_ocr_text == "30% 26.9%"
    assert pages[1].stored_text == "30% 26.9%"


def test_unpaged_items_counted_not_dropped_silently() -> None:
    _, unpaged = split_detail_by_page([{"type": "paragraph", "text": "无页 1"}])
    assert unpaged == 1


def test_excess_is_multiset_difference() -> None:
    assert excess(Counter({"1": 3, "2": 1}), Counter({"1": 1, "2": 1})) == Counter({"1": 2})


def test_heal_spacing_decimal_and_thousands() -> None:
    assert heal_spacing("增加1 . 81 个百分点") == "增加1.81 个百分点"
    assert heal_spacing("持有 5 , 223 , 600 股") == "持有 5,223,600 股"
    assert heal_spacing("第1.2节 和 3.5") == "第1.2节 和 3.5"  # 无空白不动


def test_value_key_ignores_decoration() -> None:
    assert value_key("(12.50)") == value_key("-12.5") == value_key("12.5%") == "12.5"
    assert value_key("1000") != value_key("100")


def test_classify_merge_expansion() -> None:
    cats = classify_invented("合计 100\n合计 100\n合计 100", "合计 100", "合计 100")
    assert sum(cats[CAT_MERGE_EXPANSION].values()) == 2
    assert not cats[CAT_VALUE_ABSENT]


def test_classify_spacing_artifact() -> None:
    cats = classify_invented("增加1 . 81 个百分点", "增加1 . 81 个百分点", "增加1.81个百分点")
    assert sum(cats[CAT_SPACING].values()) == 2
    assert not cats[CAT_VALUE_ABSENT]


def test_classify_image_ocr_before_format_variant() -> None:
    cats = classify_invented("0.0% 30%", "0.0% 30%", "0 10", image_text="0.0% 30%")
    assert cats[CAT_IMAGE_OCR] == Counter({"0.0%": 1, "30%": 1})
    assert not cats[CAT_FORMAT_VARIANT]


def test_classify_adjacent_page_format_and_absent() -> None:
    cats = classify_invented(
        "123 -5 999", "123 -5 999", "5 7", adjacent_layer_text="123", image_text=""
    )
    assert cats[CAT_ADJACENT_PAGE] == Counter({"123": 1})
    assert cats[CAT_FORMAT_VARIANT] == Counter({"-5": 1})
    assert cats[CAT_VALUE_ABSENT] == Counter({"999": 1})


def test_categories_partition_the_invented_total() -> None:
    stored = "合计 100\n合计 100\n3 . 5 777 888"
    unexp = "合计 100\n3 . 5 777 888"
    layer = "合计 3.5 888"
    cats = classify_invented(stored, unexp, layer, image_text="777")
    total = sum(sum(c.values()) for c in cats.values())
    # 入库数字 100,100,3,5,777,888；文字层 3.5,888 → 多余 100,100,3,5,777 共 5 个，不重不漏
    assert total == 5
    assert sum(cats[CAT_MERGE_EXPANSION].values()) == 1
    assert sum(cats[CAT_SPACING].values()) == 2
    assert sum(cats[CAT_IMAGE_OCR].values()) == 1
    assert sum(cats[CAT_VALUE_ABSENT].values()) == 1


def test_split_lost_priority_header_footer_chart_table_other() -> None:
    lost = split_lost(
        layer_text="10 20 30 40 50",
        out_text="10",
        header_footer_text="20",
        chart_text="30",
        table_word_texts=["40"],
    )
    assert lost == {"header_footer": 1, "chart": 1, "table": 1, "other": 1}


def test_examples_are_values_only_sorted_and_capped() -> None:
    c = Counter({str(n): 1 for n in range(9, 0, -1)})
    assert examples(c) == ["1", "2", "3", "4", "5"]
