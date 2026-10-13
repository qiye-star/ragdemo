from __future__ import annotations

from ragdemo.evals.grouping import (
    MIN_GROUP_SIZE,
    CaseOutcome,
    GroupMetrics,
    case_group,
    engine_group,
    summarize_groups,
)


def test_case_is_table_when_any_gold_block_is_a_table() -> None:
    assert case_group({"table", "paragraph"}, author="auto:deepseek-chat:v1") == "table/auto"
    assert case_group({"paragraph"}, author="auto:deepseek-chat:v1") == "other/auto"
    assert case_group({"figure"}, author="human:tbl-alice") == "other/human"
    assert case_group({"table"}, author="founder") == "table/human"


def test_summarize_groups_per_group_and_gating_flag() -> None:
    rows = [
        CaseOutcome("table/auto", hit10=1.0, hit50=1.0, rr10=0.5),
        CaseOutcome("table/auto", hit10=0.0, hit50=1.0, rr10=0.0),
        CaseOutcome("other/auto", hit10=1.0, hit50=1.0, rr10=1.0),
    ]
    got = summarize_groups(rows)
    assert got["table/auto"] == GroupMetrics(
        n=2, recall_at_10=0.5, recall_at_50=1.0, mrr_at_10=0.25, gated=False
    )
    assert got["other/auto"].n == 1
    big = summarize_groups([CaseOutcome("other/auto", 1.0, 1.0, 1.0)] * MIN_GROUP_SIZE)
    assert big["other/auto"].gated is True


def test_as_dict_is_json_ready() -> None:
    m = GroupMetrics(n=29, recall_at_10=14 / 29, recall_at_50=0.9, mrr_at_10=0.4, gated=True)
    assert m.as_dict() == {
        "n": 29,
        "recall_at_10": 14 / 29,
        "recall_at_50": 0.9,
        "mrr_at_10": 0.4,
        "gated": True,
    }


def test_engine_group_labels_by_parse_source_and_origin() -> None:
    assert engine_group({"local"}, author="human:zs") == "local/human"
    assert engine_group({None}, author="auto:m:v") == "legacy/auto"
    assert engine_group({"local", "textin"}, author="human:zs") == "mixed/human"
    assert engine_group(set(), author="human:zs") == "unknown/human"
