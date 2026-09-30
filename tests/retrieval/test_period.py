"""期别感知排序偏置：同文异年的块，按问题里的明确年份修正排序（默认关闭）。"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.retrieval.fusion import FusedHit
from ragdemo.retrieval.period import (
    boost_fused,
    boost_scores,
    explicit_year,
    period_factor,
)
from ragdemo.retrieval.types import RetrievalConfig
from ragdemo_core.db.session import as_of_session


def test_explicit_year_needs_exactly_one_distinct_year() -> None:
    assert explicit_year("海光信息 2023 年年度报告里主要产品有哪些？") == "2023"
    assert explicit_year("2023年第三季度报告的营业收入") == "2023"
    assert explicit_year("2022 年与 2023 年相比增长了多少？") is None  # 比较类：不偏向任何一年
    assert explicit_year("海光信息的主要产品有哪些？") is None
    assert explicit_year("公司在 12023 年之后") is None  # 不是年份


def test_period_factor_rewards_the_same_year_and_penalises_other_years_only() -> None:
    assert period_factor("2023", "海光信息2023年年度报告", 0.3) == pytest.approx(1.3)
    assert period_factor("2023", "海光信息2024年年度报告", 0.3) == pytest.approx(0.7)
    assert period_factor("2023", "关于召开业绩说明会的公告", 0.3) == 1.0  # 标题没有年份：不动
    assert period_factor("2023", "2022 年与 2023 年对比说明", 0.3) == 1.0  # 标题里有多个年份：不动


def test_default_config_keeps_the_boost_off_and_validates_the_range() -> None:
    assert RetrievalConfig().period_boost == 0.0
    RetrievalConfig(period_boost=0.4).validate()
    with pytest.raises(ValueError):
        RetrievalConfig(period_boost=1.0).validate()
    with pytest.raises(ValueError):
        RetrievalConfig(period_boost=-0.1).validate()


@pytest.mark.db
def test_boost_reorders_same_content_from_different_years(corpus: psycopg.Connection) -> None:
    corpus.execute("UPDATE core.document SET title = '寒武纪 2023 年年度报告' WHERE doc_id = 1")
    corpus.execute("UPDATE core.document SET title = '寒武纪 2024 年年度报告' WHERE doc_id = 2")
    corpus.commit()
    ids = {
        row[0]: row[1]
        for row in corpus.execute(
            "SELECT doc_id, min(block_id) FROM core.doc_block WHERE is_leaf AND doc_id IN (1, 2)"
            " GROUP BY doc_id"
        ).fetchall()
    }
    b2023, b2024 = int(ids[1]), int(ids[2])
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    with as_of_session(corpus, as_of):
        scores = {b2024: 1.00, b2023: 0.95}  # 2024 年的略高
        same = boost_scores(corpus, "2023 年年度报告里的研发情况？", scores, 0.0)
        assert same == scores  # boost=0：原样
        no_year = boost_scores(corpus, "研发情况怎么样？", scores, 0.3)
        assert no_year == scores  # 问题里没有年份：原样
        boosted = boost_scores(corpus, "2023 年年度报告里的研发情况？", scores, 0.3)
        assert boosted[b2023] > boosted[b2024]  # 问 2023：2023 年那份反超

        fused = [FusedHit(b2024, 1.0, 1, 1), FusedHit(b2023, 0.95, 2, 2)]
        out = boost_fused(corpus, "2023 年年度报告里的研发情况？", fused, 0.3)
        assert [f.block_id for f in out] == [b2023, b2024]
