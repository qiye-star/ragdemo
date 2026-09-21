"""评测 CLI 与基线比对。"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from click.testing import CliRunner
from psycopg.types.json import Jsonb

from ragdemo.cli import main
from ragdemo.evals.cli import compare_to_baseline


def test_eval_subcommands_exist() -> None:
    result = CliRunner().invoke(main, ["eval", "--help"])
    assert result.exit_code == 0
    assert "add-retrieval" in result.output
    assert "run" in result.output


def _record(conn: psycopg.Connection, sha: str, recall: float) -> None:
    conn.execute(
        "INSERT INTO evals.eval_run (run_id, suite, git_sha, config, metrics,"
        " started_at, ended_at) VALUES (%s,'retrieval',%s,%s,%s,%s,%s)",
        (
            f"r-{sha}",
            sha,
            Jsonb({}),
            Jsonb({"recall_at_10": recall}),
            datetime.now(UTC),
            datetime.now(UTC),
        ),
    )
    conn.commit()


@pytest.mark.db
def test_no_baseline_passes_the_gate(corpus: psycopg.Connection) -> None:
    """首次运行没有基线，不该因此阻断合并。"""
    ok, baseline = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.5, 0.02)
    assert ok is True
    assert baseline is None


@pytest.mark.db
def test_small_regression_within_tolerance_passes(corpus: psycopg.Connection) -> None:
    _record(corpus, "base", 0.85)
    ok, baseline = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.84, 0.02)
    assert ok is True
    assert baseline == pytest.approx(0.85)


@pytest.mark.db
def test_regression_beyond_tolerance_fails(corpus: psycopg.Connection) -> None:
    """08 §4.1：不得低于主干基线 0.02。"""
    _record(corpus, "base", 0.85)
    ok, _ = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.80, 0.02)
    assert ok is False


@pytest.mark.db
def test_improvement_passes(corpus: psycopg.Connection) -> None:
    _record(corpus, "base", 0.85)
    ok, _ = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.90, 0.02)
    assert ok is True


@pytest.mark.db
def test_baseline_is_the_most_recent_run(corpus: psycopg.Connection) -> None:
    _record(corpus, "old", 0.60)
    _record(corpus, "new", 0.85)
    _, baseline = compare_to_baseline(corpus, "retrieval", "recall_at_10", 0.84, 0.02)
    assert baseline == pytest.approx(0.85)
