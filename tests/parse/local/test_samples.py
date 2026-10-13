# tests/parse/local/test_samples.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.samples import (
    FORCED_TUNE,
    SampleDoc,
    broker_of,
    layout_from_fixture,
    layout_to_fixture,
    mask_text,
    read_split,
    research_key,
    split_samples,
    write_split,
)


def _docs() -> list[SampleDoc]:
    research = [SampleDoc("research", f"AP{i:03d}", "甲" if i < 5 else "乙") for i in range(9)]
    return [*research, SampleDoc("docs", "900", "annual_report")]


def test_split_is_deterministic_stratified_and_respects_forced_tune() -> None:
    a = split_samples(_docs(), forced_tune=frozenset({"AP000"}))
    assert a == split_samples(_docs(), forced_tune=frozenset({"AP000"}))
    by = {r.doc.key: r for r in a}
    assert by["AP000"].sample_set == "tune" and by["AP000"].forced
    # 甲：除强制项外 4 份 → 留出 2；乙：4 份 → 留出 2；单份的分层进调参集
    assert sum(r.doc.stratum == "甲" and r.sample_set == "holdout" for r in a) == 2
    assert sum(r.doc.stratum == "乙" and r.sample_set == "holdout" for r in a) == 2
    assert by["900"].sample_set == "tune" and not by["900"].forced
    assert len(a) == 10


def test_an_odd_stratum_gives_the_extra_document_to_the_tuning_set() -> None:
    docs = [SampleDoc("research", f"AP{i}", "丙") for i in range(5)]
    rows = split_samples(docs, forced_tune=frozenset())
    assert sum(r.sample_set == "holdout" for r in rows) == 2


def test_a_different_seed_gives_a_different_split() -> None:
    docs = [SampleDoc("research", f"AP{i:03d}", "丁") for i in range(40)]
    a = {
        r.doc.key
        for r in split_samples(docs, forced_tune=frozenset(), seed=1)
        if r.sample_set == "holdout"
    }
    b = {
        r.doc.key
        for r in split_samples(docs, forced_tune=frozenset(), seed=2)
        if r.sample_set == "holdout"
    }
    assert a != b and len(a) == len(b) == 20


def test_split_round_trips_through_tsv(tmp_path: Path) -> None:
    rows = split_samples(_docs(), forced_tune=frozenset({"AP000"}))
    path = tmp_path / "split.tsv"
    write_split(rows, path)
    assert read_split(path) == rows
    assert path.read_text(encoding="utf-8").splitlines()[0] == "group\tkey\tstratum\tset\tforced"


def test_forced_tune_lists_the_documents_already_shown_in_the_v1_report() -> None:
    assert {"552", "556", "560", "563", "547"} <= FORCED_TUNE
    assert "AP202503051644062530" in FORCED_TUNE and len(FORCED_TUNE) == 12


def test_research_key_and_broker_come_from_the_file_name() -> None:
    name = "2025-04-24_民生证券_2025年一季报点评：业绩高速增长_AP202504241661650341.pdf"
    assert research_key(name) == "AP202504241661650341"
    assert broker_of(name) == "民生证券"
    with pytest.raises(ValueError, match="AP"):
        research_key("no-id.pdf")


def test_mask_keeps_length_digit_shape_and_punctuation() -> None:
    assert mask_text("海光(4%)营收 91.2 亿元 AMD") == "字字(8%)字字 88.8 字字 xxx"
    assert len(mask_text("（一）业绩预告")) == len("（一）业绩预告")


def test_layout_fixture_round_trip_masks_text_and_keeps_geometry() -> None:
    lay = PageLayout(
        3, 600.0, 800.0, [Word("营收91.2", 60.0, 120.5, 100.0, 110.0, True, 10.5, True)], "营收91.2"
    )
    back = layout_from_fixture(layout_to_fixture(lay))
    assert back.page == 3 and back.text == "字字88.8"
    (w,) = back.words
    assert (w.text, w.x0, w.x1, w.top, w.bottom, w.upright, w.size, w.bold) == (
        "字字88.8",
        60.0,
        120.5,
        100.0,
        110.0,
        True,
        10.5,
        True,
    )


# ---- 以下为控制者裁决追加：夹具与拆分清单的结构性测试 ----
_FIX_ALLOWED = set("字x8") | set(
    " \t.,;:%()（）[]{}<>-—_/\|·、，。；：！？!?\"'“”‘’《》【】+=*&#@~^$￥%"
)


def _only_masked(text: str) -> bool:
    return all(ch in _FIX_ALLOWED or not ch.isalnum() for ch in text)


def _sidebar_layout() -> PageLayout:
    """合成的侧栏页：左 70% 是正文三行，右侧一条窄栏三行；用来覆盖夹具的往返与脱敏路径。"""
    words: list[Word] = []
    for i in range(3):
        words.append(
            Word("正文Body", 40.0, 380.0, 100.0 + 14 * i, 111.0 + 14 * i, True, 10.5, False)
        )
        words.append(
            Word("评级Buy 12.5", 420.0, 560.0, 100.0 + 14 * i, 111.0 + 14 * i, True, 8.0, True)
        )
    return PageLayout(1, 595.0, 842.0, words, "正文Body 评级Buy 12.5")


def test_synthetic_sidebar_fixture_is_masked_and_geometry_exact() -> None:
    fx = layout_to_fixture(_sidebar_layout())
    assert _only_masked(fx["text"]) and all(_only_masked(w[0]) for w in fx["words"])
    back = layout_from_fixture(fx)
    assert [(w.x0, w.x1, w.top, w.bottom) for w in back.words] == [
        (w.x0, w.x1, w.top, w.bottom) for w in _sidebar_layout().words
    ]
    assert fx["words"][1][0] == "字字xxx 88.8"


_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "layouts"
_LOCAL_DIR = Path(__file__).resolve().parents[3] / "data" / "experiments" / "l0v2" / "fixtures"


def _check_fixture_files(files: list[Path]) -> None:
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        lay = layout_from_fixture(d)
        assert lay.words, f.name
        assert _only_masked(lay.text) and all(_only_masked(w.text) for w in lay.words), f.name


def test_committed_announcement_fixtures_are_masked() -> None:
    files = sorted(_FIXTURE_DIR.glob("a_*.json"))
    assert len(files) == 3
    _check_fixture_files(files)


def test_local_research_fixtures_are_masked() -> None:
    files = sorted(_LOCAL_DIR.glob("r_*.json"))
    if not files:
        pytest.skip("研报脱敏夹具只在本机 data/experiments/l0v2/fixtures/，不入 git；本机未生成")
    assert len(files) == 5
    _check_fixture_files(files)


def test_committed_split_manifest_is_consistent() -> None:
    path = Path(__file__).resolve().parents[3] / "docs" / "10.12-plan" / "l0v2" / "split.tsv"
    rows = read_split(path)
    assert sum(r.doc.group == "docs" for r in rows) == 22
    assert sum(r.doc.group == "research" for r in rows) == 91
    forced = {r.doc.key for r in rows if r.forced}
    assert forced == set(FORCED_TUNE)
    assert all(r.sample_set == "tune" for r in rows if r.forced)
    assert len({(r.doc.group, r.doc.key) for r in rows}) == len(rows)


def test_tuning_commands_refuse_the_holdout_set() -> None:
    import pytest

    from ragdemo.parse.local.samples import require_tune_set

    assert require_tune_set("tune") == "tune"
    with pytest.raises(ValueError, match="留出集只由 T6 的锁定运行器运行"):
        require_tune_set("holdout")
    with pytest.raises(ValueError):
        require_tune_set("other")


def test_a_fixture_with_an_unknown_graphic_kind_is_rejected() -> None:
    fx = {
        "page": 1,
        "width": 100.0,
        "height": 100.0,
        "words": [],
        "graphics": [["blob", 0, 1, 0, 1, False, 0]],
    }
    with pytest.raises(ValueError, match="graphic"):
        layout_from_fixture(fx)
