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
    ANNOUNCEMENT_DAY,
    CST,
    HYGON,
    INSPUR,
    PRIVATE_MARKER,
    SUGON,
    T_2024Q3,
    T_ANNOUNCEMENT_CNINFO_KNOWN,
    T_ANNOUNCEMENT_WIND_KNOWN,
    T_CORRECTION,
    T_DEFINITION_V2,
    T_FUTURE_DOC,
    T_FX_1,
    T_FX_2,
    T_HOLDERS_FIRST,
    T_HOLDERS_REVISED,
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
            lambda: api.list_announcements(HYGON, as_of=naive),
            lambda: api.query_shareholders(HYGON, as_of=naive),
            lambda: api.query_segments(HYGON, as_of=naive),
            lambda: api.get_fx_rate("USD", "CNY", on=date(2025, 3, 28), as_of=naive),
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
                "mom",  # type: ignore[arg-type]
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

    def test_search_for_private_content_never_returns_it(self) -> None:
        """混合检索（BM25 + 向量）会用相近的公共块补足结果，所以不断言"空"——断言的是
        私有块与它的文本一个都不会出现。"""
        hits = self.make_api().search_blocks(PRIVATE_MARKER, as_of=LATER, k=50)
        assert all(h.block_id != 31 and h.doc_id != 3 for h in hits)
        assert all(PRIVATE_MARKER not in (h.preview or "") for h in hits)

    def test_search_hides_text_of_sources_that_cannot_show_raw(self) -> None:
        hits = self.make_api().search_blocks("DCU", as_of=AFTER_CORRECTION, k=50)
        by_id = {h.block_id: h for h in hits}
        assert by_id[21].preview is None
        assert by_id[11].preview

    def test_search_filters_and_limit(self) -> None:
        api = self.make_api()
        # 过滤是硬约束（混合检索可能用同范围内的相近块补足，所以只断言"不越界"）。
        only_quarterly = api.search_blocks("DCU", as_of=LATER, doc_types=["quarterly"], k=50)
        assert 1 in {h.doc_id for h in only_quarterly}
        assert {h.doc_id for h in only_quarterly} <= {1, 5}  # 样例里只有这两份季报
        sugon = api.search_blocks("服务器", as_of=LATER, entity_ids=[SUGON], k=50)
        assert sugon and {h.doc_id for h in sugon} == {5}
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

    # --- 公告目录（v1.1）-----------------------------------------------------------

    def test_degraded_row_is_superseded_by_the_primary_source(self) -> None:
        api = self.make_api()
        before = api.list_announcements(HYGON, as_of=T_ANNOUNCEMENT_WIND_KNOWN + timedelta(hours=1))
        assert [(a.source, a.has_full_text, a.doc_id) for a in before] == [("wind", False, None)]

        after = api.list_announcements(
            HYGON, as_of=T_ANNOUNCEMENT_CNINFO_KNOWN + timedelta(hours=1)
        )
        sources = {a.source: a for a in after}
        assert "wind" not in sources, "巨潮到达后，降级层的行不该再出现"
        assert sources["cninfo"].has_full_text and sources["cninfo"].doc_id == 100

    def test_has_full_text_false_always_has_no_doc_id(self) -> None:
        api = self.make_api()
        rows = api.list_announcements(HYGON, as_of=T_ANNOUNCEMENT_WIND_KNOWN + timedelta(hours=1))
        assert all(a.doc_id is None for a in rows if not a.has_full_text)

    def test_day_precision_and_second_precision_known_at_differ(self) -> None:
        """cninfo（日精度）当日 23:59:59 之前不可见；EDGAR（秒精度）同一天更早的
        真实受理时刻一到就可见——两者不该被同一条"当日"规则误判。"""
        api = self.make_api()
        mid_afternoon = datetime(2025, 3, 31, 20, 0, tzinfo=CST)
        rows = api.list_announcements(HYGON, as_of=mid_afternoon)
        sources = {a.source for a in rows}
        assert "edgar" in sources, "EDGAR 是秒级真实受理时刻，14:30 已过，20:00 该可见"
        assert "cninfo" not in sources, "cninfo 是日精度，当日 23:59:59 之前不可见"

        end_of_day = T_ANNOUNCEMENT_CNINFO_KNOWN + timedelta(minutes=1)
        rows = api.list_announcements(HYGON, as_of=end_of_day)
        assert "cninfo" in {a.source for a in rows}

    def test_list_announcements_filters_by_since_and_until(self) -> None:
        api = self.make_api()
        rows = api.list_announcements(
            HYGON, as_of=LATER, since=ANNOUNCEMENT_DAY, until=ANNOUNCEMENT_DAY
        )
        assert rows and all(a.publish_date == ANNOUNCEMENT_DAY for a in rows)
        assert (
            api.list_announcements(
                HYGON,
                as_of=LATER,
                since=ANNOUNCEMENT_DAY + timedelta(days=1),
            )
            == []
        )

    def test_list_announcements_orders_by_publish_date(self) -> None:
        rows = self.make_api().list_announcements(HYGON, as_of=LATER)
        assert [r.publish_date for r in rows] == sorted(r.publish_date for r in rows)
        assert self.make_api().match_rules("no_such_event", "先进封装", as_of=LATER) == []

    # --- 股东 / 分部 / 汇率（v1.2）--------------------------------------------------

    def test_shareholders_are_invisible_before_first_disclosure(self) -> None:
        api = self.make_api()
        before = T_HOLDERS_FIRST - timedelta(seconds=1)
        assert api.query_shareholders(HYGON, as_of=before) == []

    def test_shareholder_revision_keeps_history_and_cites_its_basis(self) -> None:
        api = self.make_api()
        original = api.query_shareholders(HYGON, as_of=T_HOLDERS_FIRST + timedelta(days=1))
        assert [(r.rank, r.shares) for r in original] == [
            (1, Decimal("600000000")),
            (2, Decimal("250000000")),
        ]
        assert {r.citation.known_at_basis for r in original} == {"first_periodic_report"}

        revised = api.query_shareholders(HYGON, as_of=T_HOLDERS_REVISED + timedelta(days=1))
        second = next(r for r in revised if r.rank == 2)
        assert second.shares == Decimal("251194546")
        assert second.citation.known_at_basis == "revision_observed"
        assert second.citation.known_at == T_HOLDERS_REVISED
        assert all(0 < r.ratio <= 1 for r in revised)

    def test_shareholders_filter_by_period(self) -> None:
        api = self.make_api()
        assert api.query_shareholders(HYGON, as_of=LATER, period="2024FY")
        assert api.query_shareholders(HYGON, as_of=LATER, period="2023FY") == []
        assert api.query_shareholders(SUGON, as_of=LATER) == []

    def test_segments_are_time_filtered_and_filterable_by_dimension(self) -> None:
        api = self.make_api()
        assert api.query_segments(HYGON, as_of=T_HOLDERS_FIRST - timedelta(seconds=1)) == []
        rows = api.query_segments(HYGON, as_of=LATER, period="2024FY")
        assert {r.dimension for r in rows} == {"product", "region"}
        products = api.query_segments(HYGON, as_of=LATER, dimension="product")
        assert [r.segment_name for r in products] == ["示意产品A", "示意产品B"]  # 收入降序
        assert all(r.currency == "CNY" and r.citation.source == "wind" for r in rows)

    def test_fx_rate_uses_the_latest_rate_that_was_already_published(self) -> None:
        api = self.make_api()
        on = date(2025, 3, 28)
        # 09:00 时 3-28 的中间价（09:15 公布）还不可见：只能看到 3-27 的。
        early = datetime(2025, 3, 28, 9, 0, tzinfo=CST)
        assert early < T_FX_2
        quote = api.get_fx_rate("USD", "CNY", on=on, as_of=early)
        assert quote is not None and quote.rate_date == date(2025, 3, 27)
        assert quote.rate == Decimal("7.2500") and quote.citation.known_at == T_FX_1
        later = api.get_fx_rate("USD", "CNY", on=on, as_of=T_FX_2 + timedelta(minutes=1))
        assert later is not None and later.rate == Decimal("7.2600")

    def test_fx_rate_on_a_weekend_uses_the_previous_trading_day(self) -> None:
        quote = self.make_api().get_fx_rate("USD", "CNY", on=date(2025, 3, 30), as_of=LATER)
        assert quote is not None and quote.rate_date == date(2025, 3, 28)

    def test_fx_rate_returns_none_when_missing_or_stale(self) -> None:
        api = self.make_api()
        assert api.get_fx_rate("EUR", "CNY", on=date(2025, 3, 28), as_of=LATER) is None
        assert api.get_fx_rate("USD", "CNY", on=date(2025, 6, 1), as_of=LATER) is None  # 过期
        assert api.get_fx_rate("USD", "CNY", on=date(2025, 3, 1), as_of=LATER) is None  # 尚无

    # --- 环比 / TTM（v1.2，阶段 2.5）------------------------------------------------

    def test_qoq_and_ttm_without_the_needed_periods_give_no_number_and_say_why(self) -> None:
        api = self.make_api()
        for kind in ("qoq", "ttm"):
            d = api.query_derived(
                HYGON,
                "revenue_total",
                kind,
                as_of=AFTER_CORRECTION,
                period="2024Q3",
            )
            assert d is not None and d.kind == kind
            assert d.value is None and d.note, "样例里没有 2024Q2 / 2023FY，不该编数"
            assert d.inputs and d.inputs[0].period == "2024Q3"

    def test_qoq_and_ttm_are_none_when_the_period_is_not_known_yet(self) -> None:
        api = self.make_api()
        for kind in ("qoq", "ttm"):
            assert (
                api.query_derived(
                    HYGON,
                    "revenue_total",
                    kind,
                    as_of=BEFORE_Q3,
                    period="2024Q3",
                )
                is None
            )
