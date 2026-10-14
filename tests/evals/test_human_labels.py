"""人工标注流程：导出表 → 写问题 → 校验 → 入库，且可与合成集区分、可单独评测。"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.evals.human_labels import (
    ORIGIN_NOTES,
    SHEET_COLUMNS,
    LabelSheetError,
    export_label_sheet,
    insert_labeled,
    parse_label_sheet,
)

GOOD_Q = "寒武纪云端训练芯片业务的收入情况如何？"


def _fill(path: Path, edits: dict[int, dict[str, str]]) -> None:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for i, patch in edits.items():
        rows[i].update(patch)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)


@pytest.mark.db
def test_sheet_export_has_passages_and_empty_question_column(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "s.csv"
    n = export_label_sheet(corpus, path, n=3)
    assert n >= 1
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(SHEET_COLUMNS)
    assert all(r["passage"] and r["question"] == "" for r in rows)


@pytest.mark.db
def test_only_filled_rows_are_imported_as_human_cases(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "s.csv"
    export_label_sheet(corpus, path, n=3)
    _fill(path, {0: {"question": GOOD_Q, "difficulty": "medium", "entity_filter": "Y"}})
    cases = parse_label_sheet(corpus, path)
    assert len(cases) == 1 and cases[0].question == GOOD_Q
    assert cases[0].gold_block_ids  # 源块（及同文块）
    n = insert_labeled(corpus, cases, as_of=datetime(2026, 9, 30, tzinfo=UTC), annotator="张三")
    row = corpus.execute(
        "SELECT count(*), count(*) FILTER (WHERE author = 'human:张三') FROM evals.eval_retrieval"
    ).fetchone()
    assert row == (n, n)


@pytest.mark.db
def test_model_written_cases_keep_their_own_author_and_note(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    """模型出题不得记成 human:；author 与备注如实标注，`--author-prefix blindmodel:` 只选中它们。"""
    path = tmp_path / "s.csv"
    export_label_sheet(corpus, path, n=3)
    _fill(path, {0: {"question": GOOD_Q, "difficulty": "medium"}})
    cases = parse_label_sheet(corpus, path, origin="blindmodel")
    assert cases[0].notes.startswith(ORIGIN_NOTES["blindmodel"])
    n = insert_labeled(
        corpus,
        cases,
        as_of=datetime(2026, 9, 30, tzinfo=UTC),
        annotator="tbl-v1",
        origin="blindmodel",
    )
    rows = corpus.execute(
        "SELECT author, notes FROM evals.eval_retrieval WHERE author LIKE 'blindmodel:%'"
    ).fetchall()
    assert len(rows) == n == 1 and rows[0][0] == "blindmodel:tbl-v1"
    assert "人工" not in rows[0][1]
    assert corpus.execute(
        "SELECT count(*) FROM evals.eval_retrieval WHERE author LIKE 'human:%'"
    ).fetchone() == (0,)


@pytest.mark.db
def test_blind_text_set_has_its_own_prefix_disjoint_from_blindmodel(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    """正文盲标集记为 blindmodel-text:，`--author-prefix blindmodel:` 不会把它选进来，反之亦然。"""
    path = tmp_path / "s.csv"
    export_label_sheet(corpus, path, n=3)
    _fill(path, {0: {"question": GOOD_Q, "difficulty": "medium"}})
    when = datetime(2026, 9, 30, tzinfo=UTC)
    tbl = parse_label_sheet(corpus, path, origin="blindmodel")
    insert_labeled(corpus, tbl, as_of=when, annotator="tbl-v1", origin="blindmodel")
    txt = parse_label_sheet(corpus, path, origin="blindmodel-text")
    assert txt[0].notes.startswith(ORIGIN_NOTES["blindmodel-text"])
    insert_labeled(corpus, txt, as_of=when, annotator="text-v1", origin="blindmodel-text")

    def authors(prefix: str) -> list[str]:
        rows = corpus.execute(
            "SELECT author FROM evals.eval_retrieval WHERE author LIKE %s", (f"{prefix}%",)
        ).fetchall()
        return [str(r[0]) for r in rows]

    assert authors("blindmodel:") == ["blindmodel:tbl-v1"]
    assert authors("blindmodel-text:") == ["blindmodel-text:text-v1"]


def test_unknown_origin_is_refused() -> None:
    when = datetime(2026, 9, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        insert_labeled(None, [], as_of=when, annotator="x", origin="auto")  # type: ignore[arg-type]


@pytest.mark.db
def test_problems_are_all_reported_and_nothing_is_written(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "s.csv"
    export_label_sheet(corpus, path, n=4)
    _fill(
        path,
        {
            0: {"question": "该公司的收入是多少？", "difficulty": "easy"},  # 指代不明
            1: {"question": "该不该买入寒武纪的股票？", "difficulty": "easy"},  # 买卖用语
            2: {"question": GOOD_Q, "difficulty": "very-hard"},  # 难度非法
            3: {"question": GOOD_Q, "difficulty": "easy", "extra_gold_block_ids": "999999999"},
        },
    )
    with pytest.raises(LabelSheetError) as exc:
        parse_label_sheet(corpus, path)
    msg = str(exc.value)
    assert msg.count("问题不合格") == 2 and "difficulty" in msg and "不存在或已被取代" in msg
    assert corpus.execute("SELECT count(*) FROM evals.eval_retrieval").fetchone() == (0,)


@pytest.mark.db
def test_duplicate_questions_and_an_empty_sheet_are_rejected(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "s.csv"
    export_label_sheet(corpus, path, n=2)
    with pytest.raises(LabelSheetError, match="没有任何一行填了 question"):
        parse_label_sheet(corpus, path)
    _fill(
        path,
        {
            0: {"question": GOOD_Q, "difficulty": "easy"},
            1: {"question": GOOD_Q, "difficulty": "easy"},
        },
    )
    with pytest.raises(LabelSheetError, match="问题重复"):
        parse_label_sheet(corpus, path)


@pytest.mark.db
def test_sheet_can_be_restricted_to_one_parse_source(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    leaf = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE is_leaf AND block_type = 'paragraph'"
        " ORDER BY block_id LIMIT 1"
    ).fetchone()
    assert leaf is not None
    corpus.execute(
        "UPDATE core.doc_block SET parse_source = 'local' WHERE block_id = %s", (int(leaf[0]),)
    )
    corpus.commit()
    path = tmp_path / "local.csv"
    n = export_label_sheet(corpus, path, n=5, parse_source="local")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        ids = {int(r["block_id"]) for r in csv.DictReader(fh)}
    assert n == len(ids) and ids <= {int(leaf[0])}
