"""数据接口契约 v1：任何 DataApi 实现都必须通过（docs/9.28-plan/数据接口契约-v1.md）。

用法：子类化 `DataApiContract`，实现 `make_api()` 返回一个**装载了
`ragdemo.dataapi.sample.SAMPLE`** 的实现。Mock 直接从数据集构造；真实实现把同一份
数据集种进临时库再构造——两者跑同一套断言，行为才能等价。

这里只断言契约承诺的行为（时点过滤、可见性、溯源、错误语义），不断言检索排序质量。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ragdemo.dataapi.contract import DataApi
from ragdemo.dataapi.sample import (
    CST,
    HYGON,
    INSPUR,
    PRIVATE_MARKER,
    SUGON,
    T_2024Q3,
    T_CORRECTION,
    T_DEFINITION_V2,
    T_FUTURE_DOC,
    T_NEW_RELATION,
)

BEFORE_Q3 = T_2024Q3 - timedelta(days=1)
AFTER_Q3 = T_2024Q3 + timedelta(days=1)  # 2024Q3 原值已知、尚未更正
AFTER_CORRECTION = T_CORRECTION + timedelta(days=1)
LATER = datetime(2025, 6, 1, tzinfo=CST)  # 所有样例数据都已知


class DataApiContract:
    """契约测试基类。不要在这里加 pytest fixture，子类可能有自己的。"""

    def make_api(self) -> DataApi:
        raise NotImplementedError

    # --- 规则 1：as_of 强制、带时区 -----------------------------------------------

    def test_implements_the_protocol(self) -> None:
        assert isinstance(self.make_api(), DataApi)

    def test_every_method_rejects_a_naive_as_of(self) -> None:
        api = self.make_api()
        naive = datetime(2024, 11, 1)
        calls: list[Callable[[], object]] = [
            lambda: api.resolve_entity("海光", as_of=naive),
            lambda: api.list_entities(as_of=naive),
            lambda: api.get_metric_definition("revenue_total", as_of=naive),
            lambda: api.query_fin_fact(HYGON, "revenue_total", as_of=naive),
            lambda: api.query_derived(HYGON, "revenue_total", "yoy", as_of=naive, period="2024Q3"),
            lambda: api.get_prices(
                HYGON, start=date(2024, 10, 1), end=date(2024, 10, 31), as_of=naive
            ),
            lambda: api.search_blocks("DCU", as_of=naive),
            lambda: api.get_block(11, as_of=naive),
            lambda: api.get_document(1, as_of=naive),
            lambda: api.get_relations(HYGON, as_of=naive),
            lambda: api.match_rules("capacity_expansion", "先进封装", as_of=naive),
        ]
        for call in calls:
            with pytest.raises(ValueError, match="as_of"):
                call()

    # --- 实体 ---------------------------------------------------------------------

    def test_resolve_entity_by_alias_code_and_name(self) -> None:
        api = self.make_api()
        for name in ("海光", "海光信息", "688041.SH", "海光信息技术股份有限公司", "hygon"):
            matches = api.resolve_entity(name, as_of=LATER)
            assert matches and matches[0].entity_id == HYGON, name
            assert Decimal(0) < matches[0].confidence <= Decimal(1)

    def test_resolve_unknown_name_returns_empty(self) -> None:
        assert self.make_api().resolve_entity("不存在的公司", as_of=LATER) == []

    def test_list_entities_filters_by_node(self) -> None:
        api = self.make_api()
        all_ids = {e.entity_id for e in api.list_entities(as_of=LATER)}
        assert {HYGON, SUGON, INSPUR} <= all_ids
        ai_servers = {e.entity_id for e in api.list_entities(as_of=LATER, node="AI服务器")}
        assert ai_servers == {INSPUR}

    # --- 口径 ---------------------------------------------------------------------

    def test_metric_definition_follows_its_versions(self) -> None:
        api = self.make_api()
        old = api.get_metric_definition("revenue_total", as_of=T_DEFINITION_V2 - timedelta(hours=1))
        new = api.get_metric_definition("revenue_total", as_of=T_DEFINITION_V2 + timedelta(hours=1))
        assert old is not None and new is not None
        assert (old.version, new.version) == (1, 2)
        assert old.definition != new.definition
        assert new.citation.known_at == T_DEFINITION_V2

    def test_unknown_metric_definition_is_none(self) -> None:
        assert self.make_api().get_metric_definition("no_such_metric", as_of=LATER) is None

    # --- 财务事实 -----------------------------------------------------------------

    def test_fact_is_invisible_before_it_was_known(self) -> None:
        facts = self.make_api().query_fin_fact(HYGON, "revenue_total", as_of=BEFORE_Q3)
        assert [f.period for f in facts] == ["2023Q3"]

    def test_corrected_fact_shows_original_before_and_corrected_after(self) -> None:
        api = self.make_api()
        (original,) = api.query_fin_fact(HYGON, "revenue_total", as_of=AFTER_Q3, period="2024Q3")
        (corrected,) = api.query_fin_fact(
            HYGON, "revenue_total", as_of=AFTER_CORRECTION, period="2024Q3"
        )
        assert original.value == Decimal("6000000000")
        assert corrected.value == Decimal("6100000000")
        assert original.citation.known_at == T_2024Q3
        assert corrected.citation.known_at == T_CORRECTION

    def test_every_fact_carries_a_citation_known_before_as_of(self) -> None:
        for as_of in (BEFORE_Q3, AFTER_Q3, AFTER_CORRECTION, LATER):
            for f in self.make_api().query_fin_fact(HYGON, "revenue_total", as_of=as_of):
                assert f.citation.metric_id == "revenue_total"
                assert f.citation.known_at <= as_of
                assert f.citation.snapshot_id is not None
                assert f.citation.render().startswith("[revenue_total | ")
                assert isinstance(f.value, Decimal)

    def test_facts_are_ordered_by_period_end(self) -> None:
        facts = self.make_api().query_fin_fact(HYGON, "revenue_total", as_of=LATER)
        assert [f.period_end for f in facts] == sorted(f.period_end for f in facts)

    # --- 派生值 -------------------------------------------------------------------

    def test_yoy_is_computed_from_cited_inputs(self) -> None:
        api = self.make_api()
        d = api.query_derived(
            HYGON, "revenue_total", "yoy", as_of=AFTER_CORRECTION, period="2024Q3"
        )
        assert d is not None
        assert d.value == Decimal("6100000000") / Decimal("4000000000") - 1
        assert [(f.period, f.value) for f in d.inputs] == [
            ("2024Q3", Decimal("6100000000")),
            ("2023Q3", Decimal("4000000000")),
        ]

    def test_yoy_uses_the_value_known_at_as_of(self) -> None:
        d = self.make_api().query_derived(
            HYGON, "revenue_total", "yoy", as_of=AFTER_Q3, period="2024Q3"
        )
        assert d is not None and d.value == Decimal("0.5")  # 6000 / 4000 − 1，更正前

    def test_yoy_is_none_when_the_period_is_not_known_yet(self) -> None:
        d = self.make_api().query_derived(
            HYGON, "revenue_total", "yoy", as_of=BEFORE_Q3, period="2024Q3"
        )
        assert d is None

    def test_yoy_on_a_negative_base_gives_no_number(self) -> None:
        d = self.make_api().query_derived(SUGON, "rd_expense", "yoy", as_of=LATER, period="2024Q3")
        assert d is not None and d.value is None and d.note

    def test_unknown_derived_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            self.make_api().query_derived(
                HYGON,
                "revenue_total",
                "ttm",  # type: ignore[arg-type]
                as_of=LATER,
                period="2024Q3",
            )

    # --- 行情 ---------------------------------------------------------------------

    def test_prices_are_not_known_before_the_close(self) -> None:
        api = self.make_api()
        intraday = datetime(2024, 10, 28, 10, 0, tzinfo=CST)
        bars = api.get_prices(
            HYGON, start=date(2024, 10, 1), end=date(2024, 10, 31), as_of=intraday
        )
        assert [b.trade_date for b in bars] == [date(2024, 10, 24), date(2024, 10, 25)]
        after_close = datetime(2024, 10, 28, 16, 0, tzinfo=CST)
        bars = api.get_prices(
            HYGON, start=date(2024, 10, 1), end=date(2024, 10, 31), as_of=after_close
        )
        assert bars[-1].trade_date == date(2024, 10, 28)
        assert all(isinstance(b.close, Decimal) for b in bars)

    def test_prices_reject_an_inverted_range(self) -> None:
        with pytest.raises(ValueError):
            self.make_api().get_prices(
                HYGON, start=date(2024, 10, 31), end=date(2024, 10, 1), as_of=LATER
            )

    # --- 检索与原文 ---------------------------------------------------------------

    def test_search_never_returns_private_or_future_blocks(self) -> None:
        hits = self.make_api().search_blocks("DCU", as_of=AFTER_CORRECTION, k=50)
        ids = {h.block_id for h in hits}
        assert 11 in ids and 21 in ids
        assert 31 not in ids, "私有上传材料不可见"
        assert 41 not in ids, "2025 年才公开的年报不可见"
        assert 10 not in ids, "只返回叶子块，父块用于生成不用于引用"
        for h in hits:
            assert h.citation.block_id == h.block_id and h.citation.page == h.page

    def test_future_block_appears_once_published(self) -> None:
        hits = self.make_api().search_blocks("DCU", as_of=T_FUTURE_DOC + timedelta(days=1), k=50)
        assert 41 in {h.block_id for h in hits}

    def test_search_for_private_content_finds_nothing(self) -> None:
        assert self.make_api().search_blocks(PRIVATE_MARKER, as_of=LATER) == []

    def test_search_hides_text_of_sources_that_cannot_show_raw(self) -> None:
        hits = self.make_api().search_blocks("DCU", as_of=AFTER_CORRECTION, k=50)
        by_id = {h.block_id: h for h in hits}
        assert by_id[21].preview is None
        assert by_id[11].preview

    def test_search_filters_and_limit(self) -> None:
        api = self.make_api()
        only_quarterly = api.search_blocks("DCU", as_of=LATER, doc_types=["quarterly"], k=50)
        assert {h.doc_id for h in only_quarterly} == {1}
        sugon = api.search_blocks("服务器", as_of=LATER, entity_ids=[SUGON], k=50)
        assert {h.doc_id for h in sugon} == {5}
        assert len(api.search_blocks("DCU", as_of=LATER, k=1)) == 1
        with pytest.raises(ValueError):
            api.search_blocks("DCU", as_of=LATER, k=0)

    def test_get_block_returns_content_and_parent_with_citation(self) -> None:
        b = self.make_api().get_block(11, as_of=AFTER_Q3)
        assert b is not None and b.content and "DCU" in b.content
        assert b.parent_content and "服务器 CPU" in b.parent_content
        assert b.citation.render() == "[11 | 3]"
        assert b.document.doc_id == 1 and b.document.can_show_raw

    def test_get_block_withholds_text_when_source_cannot_show_raw(self) -> None:
        b = self.make_api().get_block(21, as_of=AFTER_Q3)
        assert b is not None
        assert b.content is None and b.content_desc is None and b.parent_content is None
        assert b.citation.block_id == 21

    def test_private_and_future_blocks_and_documents_are_none(self) -> None:
        api = self.make_api()
        assert api.get_block(31, as_of=LATER) is None
        assert api.get_document(3, as_of=LATER) is None
        assert api.get_block(41, as_of=AFTER_CORRECTION) is None
        assert api.get_document(4, as_of=AFTER_CORRECTION) is None
        assert api.get_document(4, as_of=T_FUTURE_DOC + timedelta(days=1)) is not None

    # --- 产业链 -------------------------------------------------------------------

    def test_relations_respect_as_of_and_type(self) -> None:
        api = self.make_api()
        before = api.get_relations(HYGON, as_of=T_NEW_RELATION - timedelta(days=1))
        assert {(r.from_entity, r.to_entity) for r in before} == {(HYGON, SUGON), (SUGON, HYGON)}
        after = api.get_relations(HYGON, as_of=LATER)
        assert (HYGON, INSPUR) in {(r.from_entity, r.to_entity) for r in after}
        supplies = api.get_relations(HYGON, as_of=LATER, relation_type="supplies_to")
        assert {r.relation_type for r in supplies} == {"supplies_to"}

    def test_manual_relations_are_marked_by_source(self) -> None:
        rels = self.make_api().get_relations(HYGON, as_of=LATER, relation_type="supplies_to")
        assert all(r.citation.source.startswith("manual:") for r in rels)

    def test_match_rules_returns_only_enabled_rules(self) -> None:
        rules = self.make_api().match_rules("capacity_expansion", "先进封装", as_of=LATER)
        assert [r.rule_id for r in rules] == [1]
        assert self.make_api().match_rules("no_such_event", "先进封装", as_of=LATER) == []
