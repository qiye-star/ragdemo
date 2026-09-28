"""契约测试与 Mock 共用的样例数据集。

数值是**示意数据**，不是海光信息的真实财务数字——它们只用来覆盖契约要守的每一种
时点与可见性边界：

- 事实被更正（2024Q3 营收在 11-10 更正）：更正前的 as_of 看到原值，之后看到新值；
- 口径改版（营业收入定义 2024-12-01 出第 2 版）；
- 未来才公开的文档（2025-03-01）与未来才录入的关系（2025-01-01）；
- 私有上传材料（owner_user = u1）：任何接口都不可见；
- 不可展示原文的来源（can_show_raw = false）：能检索到，但不给正文；
- 被停用的传导规则：不返回。

将来真实实现接上时，把同一份数据集种进临时库，跑同一套契约测试
（tests/contracts/dataapi_contract.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from ragdemo.dataapi.contract import PropagationRule

CST = timezone(timedelta(hours=8))


def _eod(d: date) -> datetime:
    """公告类数据只有日期精度：取当日 23:59:59（北京时间），见 docs/03 §1.3。"""
    return datetime.combine(d, time(23, 59, 59), tzinfo=CST)


BASELINE = datetime(2000, 1, 1, tzinfo=CST)
T_2023Q3 = _eod(date(2023, 10, 20))
T_2024Q3 = _eod(date(2024, 10, 25))
T_CORRECTION = _eod(date(2024, 11, 10))
T_DEFINITION_V2 = datetime(2024, 12, 1, 10, 0, tzinfo=CST)
T_NEW_RELATION = datetime(2025, 1, 1, tzinfo=CST)
T_FUTURE_DOC = _eod(date(2025, 3, 1))


@dataclass(frozen=True)
class SampleEntity:
    entity_id: str
    name_full: str
    name_short: str
    market: str
    tushare_code: str
    l3_nodes: tuple[str, ...]
    primary_node: str
    aliases: tuple[str, ...]
    known_at: datetime = BASELINE


@dataclass(frozen=True)
class SampleMetricVersion:
    metric_id: str
    metric_name: str
    definition: str
    unit: str
    frequency: str
    version: int
    author: str
    known_at: datetime
    superseded_at: datetime | None


@dataclass(frozen=True)
class SampleFact:
    entity_id: str
    metric_id: str
    period: str
    period_end: date
    value: Decimal
    unit: str
    known_at: datetime
    superseded_at: datetime | None
    snapshot_id: int
    source: str = "tushare"
    currency: str = "CNY"


@dataclass(frozen=True)
class SamplePrice:
    entity_id: str
    trade_date: date
    close: Decimal

    @property
    def known_at(self) -> datetime:
        return datetime.combine(self.trade_date, time(15, 30), tzinfo=CST)  # A 股收盘


@dataclass(frozen=True)
class SampleDocument:
    doc_id: int
    entity_id: str | None
    doc_type: str
    title: str
    publish_at: datetime
    source: str
    can_show_raw: bool
    owner_user: str | None
    page_count: int
    source_url: str | None = None

    @property
    def known_at(self) -> datetime:
        return self.publish_at


@dataclass(frozen=True)
class SampleBlock:
    block_id: int
    doc_id: int
    block_type: str
    page: int | None
    section_path: str
    content: str
    is_leaf: bool
    parent_block_id: int | None = None
    content_desc: str | None = None
    bbox: tuple[Decimal, Decimal, Decimal, Decimal] | None = None


@dataclass(frozen=True)
class SampleRelation:
    from_entity: str
    to_entity: str
    relation_type: str
    strength: Decimal
    confidence: str
    known_at: datetime
    source: str = "manual:seed"
    evidence_blocks: tuple[int, ...] = ()


@dataclass(frozen=True)
class SampleRule:
    rule: PropagationRule
    enabled: bool


@dataclass(frozen=True)
class SampleDataset:
    entities: tuple[SampleEntity, ...]
    metrics: tuple[SampleMetricVersion, ...]
    facts: tuple[SampleFact, ...]
    prices: tuple[SamplePrice, ...]
    documents: tuple[SampleDocument, ...]
    blocks: tuple[SampleBlock, ...]
    relations: tuple[SampleRelation, ...]
    rules: tuple[SampleRule, ...]


HYGON = "CN.688041"
SUGON = "CN.603019"
INSPUR = "CN.000977"
PRIVATE_MARKER = "UNIQUEPRIVATEMARK"

SAMPLE = SampleDataset(
    entities=(
        SampleEntity(
            HYGON,
            "海光信息技术股份有限公司",
            "海光信息",
            "SSE",
            "688041.SH",
            ("通用服务器CPU", "云端训练芯片"),
            "通用服务器CPU",
            ("海光", "Hygon"),
        ),
        SampleEntity(
            SUGON,
            "曙光信息产业股份有限公司",
            "中科曙光",
            "SSE",
            "603019.SH",
            ("通用服务器",),
            "通用服务器",
            ("曙光",),
        ),
        SampleEntity(
            INSPUR,
            "浪潮电子信息产业股份有限公司",
            "浪潮信息",
            "SZSE",
            "000977.SZ",
            ("AI服务器", "通用服务器"),
            "AI服务器",
            ("浪潮",),
        ),
    ),
    metrics=(
        SampleMetricVersion(
            "revenue_total",
            "营业收入",
            "合并报表口径营业收入",
            "CNY",
            "quarterly",
            1,
            "seed",
            BASELINE,
            T_DEFINITION_V2,
        ),
        SampleMetricVersion(
            "revenue_total",
            "营业收入",
            "合并报表口径营业收入（含其他业务收入）",
            "CNY",
            "quarterly",
            2,
            "founder",
            T_DEFINITION_V2,
            None,
        ),
        SampleMetricVersion(
            "rd_expense",
            "研发费用",
            "合并利润表研发费用",
            "CNY",
            "quarterly",
            1,
            "seed",
            BASELINE,
            None,
        ),
    ),
    facts=(
        SampleFact(
            HYGON,
            "revenue_total",
            "2023Q3",
            date(2023, 9, 30),
            Decimal("4000000000"),
            "CNY",
            T_2023Q3,
            None,
            101,
        ),
        SampleFact(
            HYGON,
            "revenue_total",
            "2024Q3",
            date(2024, 9, 30),
            Decimal("6000000000"),
            "CNY",
            T_2024Q3,
            T_CORRECTION,
            102,
        ),
        SampleFact(
            HYGON,
            "revenue_total",
            "2024Q3",
            date(2024, 9, 30),
            Decimal("6100000000"),
            "CNY",
            T_CORRECTION,
            None,
            103,
        ),
        SampleFact(
            HYGON,
            "rd_expense",
            "2024Q3",
            date(2024, 9, 30),
            Decimal("2000000000"),
            "CNY",
            T_2024Q3,
            None,
            102,
        ),
        # 上年同期为负：同比应给不出数（见 metrics/derived.py）
        SampleFact(
            SUGON,
            "rd_expense",
            "2023Q3",
            date(2023, 9, 30),
            Decimal("-10"),
            "CNY",
            T_2023Q3,
            None,
            104,
        ),
        SampleFact(
            SUGON,
            "rd_expense",
            "2024Q3",
            date(2024, 9, 30),
            Decimal("20"),
            "CNY",
            T_2024Q3,
            None,
            105,
        ),
    ),
    prices=(
        SamplePrice(HYGON, date(2024, 10, 24), Decimal("120.50")),
        SamplePrice(HYGON, date(2024, 10, 25), Decimal("123.10")),
        SamplePrice(HYGON, date(2024, 10, 28), Decimal("125.00")),
        SamplePrice(SUGON, date(2024, 10, 28), Decimal("60.00")),
    ),
    documents=(
        SampleDocument(
            1,
            HYGON,
            "quarterly",
            "海光信息 2024 年第三季度报告",
            T_2024Q3,
            "cninfo",
            True,
            None,
            12,
            "https://example.invalid/1",
        ),
        SampleDocument(
            2,
            HYGON,
            "research",
            "第三方机构点评（原文不可展示）",
            T_2024Q3,
            "vendor-restricted",
            False,
            None,
            4,
        ),
        SampleDocument(3, HYGON, "user_upload", "用户私有材料", T_2024Q3, "user", True, "u1", 2),
        SampleDocument(
            4,
            HYGON,
            "annual_report",
            "海光信息 2024 年年度报告",
            T_FUTURE_DOC,
            "cninfo",
            True,
            None,
            200,
        ),
        SampleDocument(
            5,
            SUGON,
            "quarterly",
            "中科曙光 2024 年第三季度报告",
            T_2024Q3,
            "cninfo",
            True,
            None,
            10,
        ),
    ),
    blocks=(
        SampleBlock(
            10,
            1,
            "paragraph",
            3,
            "第二节 > 经营情况",
            "报告期内 DCU 与 CPU 业务均有增长。DCU 产品收入同比增长，服务器 CPU 出货稳定。",
            is_leaf=False,
        ),
        SampleBlock(
            11,
            1,
            "paragraph",
            3,
            "第二节 > 经营情况",
            "报告期内 DCU 产品收入同比增长。",
            is_leaf=True,
            parent_block_id=10,
            bbox=(Decimal("0.1"), Decimal("0.2"), Decimal("0.9"), Decimal("0.3")),
        ),
        SampleBlock(
            12,
            1,
            "table",
            5,
            "第三节 > 主要会计数据",
            "营业收入 6,000,000,000 元",
            is_leaf=True,
            content_desc="主要会计数据表，列出本期营业收入",
        ),
        SampleBlock(21, 2, "paragraph", 1, "点评", "预计 DCU 出货量继续提升。", is_leaf=True),
        SampleBlock(31, 3, "paragraph", 1, "", f"私有材料 DCU 机密 {PRIVATE_MARKER}", is_leaf=True),
        SampleBlock(
            41, 4, "paragraph", 8, "第二节", "2024 年 DCU 全年收入大幅增长。", is_leaf=True
        ),
        SampleBlock(
            51, 5, "paragraph", 2, "第二节", "公司服务器业务受益于 AI 需求。", is_leaf=True
        ),
    ),
    relations=(
        SampleRelation(
            HYGON, SUGON, "supplies_to", Decimal("0.9"), "medium", _eod(date(2022, 8, 12))
        ),
        SampleRelation(
            SUGON,
            HYGON,
            "invests_in",
            Decimal("0.9"),
            "high",
            _eod(date(2022, 8, 12)),
            source="announcement",
        ),
        SampleRelation(HYGON, INSPUR, "supplies_to", Decimal("0.6"), "low", T_NEW_RELATION),
    ),
    rules=(
        SampleRule(
            PropagationRule(
                1,
                "先进封装",
                "capacity_expansion",
                "云端训练芯片",
                1,
                90,
                "CoWoS 产能是 AI 加速卡出货的硬瓶颈",
                "share_estimate",
                "high",
                "founder",
            ),
            enabled=True,
        ),
        SampleRule(
            PropagationRule(
                2,
                "先进封装",
                "capacity_expansion",
                "封装设备",
                1,
                60,
                "新增封装产能要先采购设备（已停用）",
                None,
                "high",
                "founder",
            ),
            enabled=False,
        ),
        SampleRule(
            PropagationRule(
                3,
                "晶圆代工",
                "price_increase",
                "云端训练芯片",
                -1,
                30,
                "代工涨价压缩下游毛利",
                None,
                "medium",
                "founder",
            ),
            enabled=True,
        ),
    ),
)
