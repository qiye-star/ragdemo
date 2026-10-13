from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from ragdemo.parse.local.compare import HeadingMatchV2
from ragdemo.parse.local.l0_eval import (
    C_TEXTIN_MEASURED,
    LABEL_HEADER,
    EvalRow,
    FigureLabelTotals,
    HeadingTotals,
    L0Metrics,
    MulticolRates,
    Prices,
    aggregate_multicol,
    blended_cost_per_page,
    claim_holdout,
    content_audit,
    cost_breakdown,
    evaluate_gates,
    format_report,
    is_go,
    is_go_holdout2,
    multicolumn_rates,
    page_has_table_rows,
    parse_labels,
    read_figure_labels,
    read_judgments,
    read_multicol_labels,
    read_spotcheck,
    routing_cost_inputs,
    sample_items,
    textin_page_markdowns,
    textin_page_tables,
)


def _row(group: str, key: str, page: int, label: str, detected: bool) -> EvalRow:
    return EvalRow(group, key, page, label, detected)


def test_rates_exclude_unsure_and_split_by_group() -> None:
    rows = [
        _row("research", "r1", 1, "multi", True),
        _row("research", "r1", 2, "multi", False),
        _row("research", "r2", 1, "multi", True),
        _row("research", "r2", 2, "multi", True),
        _row("research", "r3", 1, "single", True),
        _row("research", "r3", 2, "single", False),
        _row("research", "r4", 1, "unsure", True),
        _row("docs", "d1", 1, "single", False),
        _row("docs", "d1", 2, "single", False),
        _row("docs", "d1", 3, "single", False),
        _row("docs", "d1", 4, "single", True),
    ]
    rep = aggregate_multicol(rows)
    assert (rep.overall.multi, rep.overall.single, rep.overall.unsure) == (4, 6, 1)
    assert rep.overall.miss_rate == 0.25
    assert rep.overall.false_alarm_rate == pytest.approx(2 / 6)
    assert rep.overall.missed == (("r1", 2),)
    assert rep.overall.false_alarms == (("d1", 4), ("r3", 1))
    assert rep.by_group["docs"].false_alarm_rate == 0.25
    assert rep.by_group["docs"].miss_rate == 0.0  # 该组没有 multi：不除零
    assert rep.by_group["research"].miss_rate == 0.25
    assert rep.confusion[("multi", False)] == 1 and rep.confusion[("unsure", True)] == 1


def test_format_report_lists_only_keys_and_pages_and_is_deterministic() -> None:
    rows = [_row("research", "AP1", 3, "multi", False), _row("docs", "266", 2, "single", True)]
    out = format_report(aggregate_multicol(rows))
    assert out == format_report(aggregate_multicol(list(reversed(rows))))
    assert "missed: AP1:p3" in out and "false_alarm: 266:p2" in out
    assert "unsure_excluded=0" in out


def test_parse_labels_reads_note_column_and_rejects_a_bad_header() -> None:
    tab, nl = chr(9), chr(10)
    rows = [
        tab.join(LABEL_HEADER),
        tab.join(["tune", "docs", "266", "1", "single", "model"]),
        tab.join(["tune", "docs", "266", "2", "multi"]),
    ]
    text = nl.join(rows)
    labels = parse_labels(text)
    assert [(r.key, r.page, r.label, r.note) for r in labels] == [
        ("266", 1, "single", "model"),
        ("266", 2, "multi", ""),
    ]
    with pytest.raises(ValueError):
        parse_labels("a\tb\n1\t2\n")


@pytest.mark.parametrize("name", ["holdout", "holdout2"])
def test_multicol_eval_refuses_holdout_sets_before_touching_data(name: str) -> None:
    spec = importlib.util.spec_from_file_location("l0v2_script", Path("scripts/l0v2.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit, match="留出集"):
        mod.main(["multicol-eval", "--set", name])


COUNTS = {
    "l0_accepted": 50,
    "table": 20,
    "multicolumn": 10,
    "chart_unresolved": 10,
    "closure_failed": 0,
    "financial_statement": 5,
    "no_text_layer": 5,
}


def test_textin_pages_rebuild_titles_tables_and_drop_vendor_headers() -> None:
    payload = {
        "result": {
            "detail": [
                {"page_id": 0, "type": "paragraph", "text": "页眉 688041", "content": 1},
                {
                    "page_id": 0,
                    "type": "paragraph",
                    "text": "一、本期业绩预告情况",
                    "outline_level": 1,
                },
                {"page_id": 0, "type": "paragraph", "text": "营业收入 91.2 亿元"},
                {"page_id": 0, "type": "image", "sub_type": "chart", "text": "40% 30% 2021 2022"},
                {
                    "page_id": 1,
                    "type": "table",
                    "cells": [
                        {"row": 0, "col": 0, "text": "项目"},
                        {"row": 0, "col": 1, "text": "2024"},
                        {"row": 1, "col": 0, "text": "收入"},
                        {"row": 1, "col": 1, "text": "91.2"},
                    ],
                },
            ]
        }
    }
    pages = textin_page_markdowns(payload)
    assert pages[1] == "## 一、本期业绩预告情况\n\n营业收入 91.2 亿元"  # figure 块不进参考
    assert pages[2].splitlines()[0] == "| 项目 | 2024 |"
    assert textin_page_tables(payload) == {2: pages[2]}


def test_content_audit_compares_content_character_multisets() -> None:
    assert content_audit("# 收入 91.2\n\n亿元", ["收入", "91.2", "亿元"])
    assert not content_audit("收入 91.3 亿元", ["收入", "91.2", "亿元"])


def test_page_has_table_rows_uses_the_data_row_rule() -> None:
    assert page_has_table_rows("| 项目 | 2024 | 2023 |\n|---|---|---|\n| 收入 | 91.2 | 60.1 |")
    assert not page_has_table_rows(
        "| 项目 | 2024 |\n|---|---|\n| 收入 | 91.2 |"
    )  # 数据行只有 1 个数字


def test_multicolumn_rates_count_accepted_multi_pages_as_misses() -> None:
    labels = {
        ("r", "A", 1): "multi",
        ("r", "B", 1): "multi",
        ("r", "C", 1): "single",
        ("r", "D", 1): "single",
        ("r", "E", 1): "unsure",
    }
    kinds = {
        k: v
        for k, v in [
            (("r", "A", 1), "multicolumn"),
            (("r", "B", 1), "plain_text"),
            (("r", "C", 1), "multicolumn"),
            (("r", "D", 1), "plain_text"),
            (("r", "E", 1), "plain_text"),
        ]
    }
    accepted = {
        ("r", "A", 1): False,
        ("r", "B", 1): True,
        ("r", "C", 1): False,
        ("r", "D", 1): True,
        ("r", "E", 1): True,
    }
    m = multicolumn_rates(labels, kinds, accepted)  # type: ignore[arg-type]
    assert m == MulticolRates(n_multi=2, missed=1, n_single=2, false_positive=1)
    assert (m.miss_rate, m.fp_rate) == (0.5, 0.5)
    with pytest.raises(ValueError, match="没有运行结果"):
        multicolumn_rates({("r", "Z", 9): "multi"}, kinds, accepted)


def test_heading_totals_are_micro_sums() -> None:
    t = HeadingTotals().add(HeadingMatchV2(2, 3, 4, 1, 1)).add(HeadingMatchV2(1, 1, 1, 0, 0))
    assert (t.recall, t.precision, t.relative_level_agreement) == (3 / 5, 3 / 4, 1.0)


def test_blended_cost_matches_the_hand_computed_example() -> None:
    assert blended_cost_per_page(COUNTS, Prices(c_textin=0.14)) == pytest.approx(0.07)
    p = Prices(c_textin=0.14, c_local=0.01, l1_enabled=True, l1_pass=0.6)
    assert blended_cost_per_page(COUNTS, p) == pytest.approx(0.0404)
    assert cost_breakdown(COUNTS, Prices(c_textin=0.14))["table"] == pytest.approx(20 * 0.14 / 100)
    assert pytest.approx(0.13556, abs=1e-5) == C_TEXTIN_MEASURED


def test_cost_agrees_with_the_tiered_routing_plan_formula() -> None:
    scan, stmt, p0 = routing_cost_inputs(COUNTS)
    c = 0.14
    # 分级路由计划 T7 blended_cost_per_page：L0 开（c_l0 = 0）、L1 / L2 关
    routing = (1 - scan) * stmt * c + (1 - scan) * (1 - stmt) * (1 - p0) * c + scan * c
    assert routing == pytest.approx(blended_cost_per_page(COUNTS, Prices(c_textin=c)))
    assert (scan, stmt, p0) == pytest.approx((0.05, 5 / 95, 50 / 90))


def _passing(**over: object) -> L0Metrics:
    base: dict[str, object] = dict(
        closure_failed=0,
        audit_inconsistent=0,
        num_recall=0.995,
        unexplained_missing=0,
        multicol=MulticolRates(40, 1, 200, 10),
        heading=HeadingTotals(70, 80, 100, 50, 55),
        research_heading_available=120,
        research_heading_judged=100,
        research_heading_precision=0.9,
        leakage_rate=0.02,
        spot_available=60,
        spot_pages=30,
        spot_order_failures=0,
        coverage_announcement=0.4,
        coverage_research=0.2,
        multicol_share_announcement=0.05,
        seconds_p95=0.4,
        figure_pages_announcement=12,
        figure_page_recall=0.995,
        figure_table_number_loss=0.0,
        figure_labels=FigureLabelTotals(
            pages=80,
            charts_total=40,
            charts_covered=36,
            boxes=45,
            boxes_chart=40,
            boxes_table_or_body=0,
        ),
    )
    base.update(over)
    return L0Metrics(**base)  # type: ignore[arg-type]


def test_all_gates_pass_on_good_metrics_and_any_single_failure_is_no_go() -> None:
    assert is_go(evaluate_gates(_passing(), final=True))
    for over in [
        dict(closure_failed=1),
        dict(audit_inconsistent=1),
        dict(num_recall=0.98),
        dict(unexplained_missing=1),
        dict(multicol=MulticolRates(40, 3, 200, 10)),
        dict(multicol=MulticolRates(20, 0, 200, 10)),
        dict(multicol=None),
        dict(heading=HeadingTotals(50, 80, 100, 50, 55)),
        dict(research_heading_precision=0.8),
        dict(research_heading_judged=40),
        dict(leakage_rate=0.06),
        dict(spot_order_failures=1),
        dict(spot_pages=10),
        dict(coverage_research=0.1),
        dict(multicol_share_announcement=0.2),
        dict(seconds_p95=2.0),
        dict(figure_page_recall=0.98),
        dict(figure_table_number_loss=0.02),
        dict(figure_labels=FigureLabelTotals(80, 40, 30, 45, 40, 0)),  # 召回 0.75
        dict(figure_labels=FigureLabelTotals(80, 40, 36, 45, 40, 1)),  # 框住了表格 / 正文
        dict(figure_labels=FigureLabelTotals(80, 40, 36, 45, 30, 0)),  # 精确率 0.667
        dict(figure_labels=None),
    ]:
        assert not is_go(evaluate_gates(_passing(**over), final=True)), over


def test_human_gates_are_not_evaluated_on_the_tuning_set_and_g7_is_na_when_few_headings() -> None:
    tune = evaluate_gates(
        _passing(
            research_heading_judged=0,
            research_heading_precision=None,
            spot_pages=0,
            spot_order_failures=None,
            figure_labels=None,
        ),
        final=False,
    )
    assert is_go(tune)
    na = evaluate_gates(
        _passing(
            research_heading_available=12,
            research_heading_judged=0,
            research_heading_precision=None,
        ),
        final=True,
    )
    assert is_go(na)


def test_figure_gates_are_not_applicable_below_their_minimum_counts() -> None:
    few = _passing(
        figure_pages_announcement=5,
        figure_page_recall=None,
        figure_labels=FigureLabelTotals(80, 12, 6, 10, 5, 0),
    )  # 图表 < 30、框 < 20
    assert is_go(evaluate_gates(few, final=True))
    bad = _passing(figure_labels=FigureLabelTotals(80, 12, 6, 10, 5, 1))  # G14b 没有"不适用"
    assert not is_go(evaluate_gates(bad, final=True))


def test_figure_labels_are_summed_and_blank_rows_rejected(tmp_path: Path) -> None:
    p = tmp_path / "f.tsv"
    p.write_text(
        "set\tgroup\tkey\tpage\tboxes\tcharts_total\tcharts_covered\tbox_labels\tnote\n"
        "holdout\tresearch\tAP1\t2\t3\t2\t2\tchart,partial,other\t\n"
        "holdout\tdocs\t556\t1\t1\t0\t0\ttable\t\n"
        "holdout\tresearch\tAP2\t1\t0\t1\t0\t\tunsure\n"
        "tune\tresearch\tAP3\t1\t0\t\t\t\t\n",
        encoding="utf-8",
    )
    t = read_figure_labels(p, "holdout")
    assert t == FigureLabelTotals(
        pages=2, charts_total=2, charts_covered=2, boxes=4, boxes_chart=2, boxes_table_or_body=1
    )
    assert (t.recall, t.precision) == (1.0, 0.5)
    with pytest.raises(ValueError, match="未标注"):
        read_figure_labels(p, "tune")
    bad = tmp_path / "b.tsv"
    bad.write_text(
        "set\tgroup\tkey\tpage\tboxes\tcharts_total\tcharts_covered\tbox_labels\tnote\n"
        "holdout\tresearch\tAP1\t2\t2\t1\t1\tchart\t\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="框数"):
        read_figure_labels(bad, "holdout")


def test_the_holdout_can_be_claimed_only_once(tmp_path: Path) -> None:
    lock = tmp_path / "holdout-run.lock"
    claim_holdout(lock, git_sha="abc", today=date(2026, 10, 20))
    assert lock.read_text(encoding="utf-8") == "2026-10-20 abc\n"
    with pytest.raises(SystemExit, match="只跑一次"):
        claim_holdout(lock, git_sha="def", today=date(2026, 10, 21))


def test_label_readers_reject_blank_labels_and_count_judgments(tmp_path: Path) -> None:
    labels = tmp_path / "l.tsv"
    labels.write_text(
        "set\tgroup\tkey\tpage\tlabel\tnote\nholdout\tresearch\tAP1\t1\tmulti\t\ntune\tdocs\t556\t1\t\t\n",
        encoding="utf-8",
    )
    assert read_multicol_labels(labels, "holdout") == {("research", "AP1", 1): "multi"}
    with pytest.raises(ValueError, match="未标注"):
        read_multicol_labels(labels, "tune")
    j = tmp_path / "j.tsv"
    j.write_text(
        "key\tpage\tline\tlevel\theading\tmd_path\tjudgment\tnote\nA\t1\t3\t2\t一、x\tp\tyes\t\nA\t1\t9\t3\ty\tp\tno\t\nA\t2\t1\t2\tz\tp\tunsure\t\n",
        encoding="utf-8",
    )
    assert read_judgments(j) == (1, 1)
    s = tmp_path / "s.tsv"
    s.write_text(
        "group\tkey\tpage\tmd_path\torder_ok\tpara_ok\tnote\ndocs\t556\t1\tp\tyes\tno\t\nresearch\tAP1\t2\tp\tno\tyes\t\n",
        encoding="utf-8",
    )
    assert read_spotcheck(s) == (2, 1)


def test_sampling_is_deterministic_and_capped() -> None:
    items = list(range(50))
    assert sample_items(items, 10, 20261013) == sample_items(items, 10, 20261013)
    assert sorted(sample_items(items, 100, 1)) == items


def _load_script() -> ModuleType:  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("l0v2_script", Path("scripts/l0v2.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["holdout", "holdout2"])
def test_a_second_run_on_a_locked_set_is_refused_before_any_data_is_read(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    lock = tmp_path / f"{name}.lock"  # 只用临时锁：永远不碰真实的 holdout-run.lock
    lock.write_text("2026-10-20 abc" + chr(10), encoding="utf-8")
    monkeypatch.setattr(mod, "RUN_LOCKS", {name: lock})
    monkeypatch.delenv(
        "RAGDEMO_DSN", raising=False
    )  # 若越过锁去读库，会因缺少 DSN 而是 KeyError，不是 SystemExit
    with pytest.raises(SystemExit, match="只跑一次"):
        mod.cmd_run(name)
    assert lock.read_text(encoding="utf-8") == "2026-10-20 abc" + chr(10)


def test_the_two_one_shot_locks_are_distinct_files() -> None:
    mod = _load_script()
    assert set(mod.RUN_LOCKS) == {"holdout", "holdout2"}
    assert len(set(mod.RUN_LOCKS.values())) == 2
    assert mod.RUN_LOCKS["holdout"].name == "holdout-run.lock"


def test_holdout2_blocks_only_on_the_three_provenance_gates() -> None:
    bad = evaluate_gates(
        _passing(heading=HeadingTotals(10, 80, 100, 5, 55), coverage_research=0.0), final=False
    )
    assert not is_go(bad) and is_go_holdout2(bad)  # 其余门槛未过也不阻断
    for over in [dict(closure_failed=1), dict(audit_inconsistent=1), dict(unexplained_missing=1)]:
        assert not is_go_holdout2(evaluate_gates(_passing(**over), final=False)), over
