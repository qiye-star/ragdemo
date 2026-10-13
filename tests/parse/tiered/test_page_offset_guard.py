from __future__ import annotations

from ragdemo.parse.tiered.page_offset_guard import page_offset_problem


def _payload(ids: list[int], metrics: list[int] | None) -> dict[str, object]:
    return {
        "result": {"detail": [{"page_id": i, "type": "paragraph", "text": "x"} for i in ids]},
        "metrics": None if metrics is None else [{"page_id": m} for m in metrics],
    }


def test_consistent_one_based_and_zero_based_payloads_pass() -> None:
    assert page_offset_problem(_payload([1, 2], [1, 2]), 2) is None
    assert page_offset_problem(_payload([0, 1], [0, 1]), 2) is None


def test_zero_based_payload_whose_first_page_has_no_detail_is_caught() -> None:
    # 首页无条目：detail 最小 page_id 是 1，会被猜成 1 基；metrics 证明是 0 基。
    msg = page_offset_problem(_payload([1], [0, 1]), 2)
    assert msg is not None and "页码基" in msg


def test_without_metrics_ids_beyond_the_sub_document_are_caught() -> None:
    assert page_offset_problem(_payload([1, 2], None), 2) is None
    assert page_offset_problem(_payload([2, 3], None), 2) is not None
    assert page_offset_problem(_payload([], None), 2) is None
