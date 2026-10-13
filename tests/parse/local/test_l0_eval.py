from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ragdemo.parse.local.l0_eval import (
    LABEL_HEADER,
    EvalRow,
    aggregate_multicol,
    format_report,
    parse_labels,
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
