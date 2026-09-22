"""适配器协议与归一化数据结构。

设计要点：known_at() 是适配器的方法而不是中间件的逻辑。每个数据源的
「现实中最早可获知的时刻」规则不同——公告用发布时间、财务用公告日、
行情用收盘时刻——只有适配器知道原始字段的语义。
见 docs/03-point-in-time.md §1.3。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


def require_aware(value: datetime, field_name: str) -> datetime:
    """强制 datetime 带时区。naive datetime 的含义随服务器时区变化，必须在入口拒绝。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须带时区，收到 naive datetime: {value!r}")
    return value


def reject_future(value: datetime, field_name: str) -> datetime:
    """known_at 落在未来意味着「事情还没发生我们就知道了」。"""
    if value > datetime.now(UTC):
        raise ValueError(f"{field_name} 落在未来: {value!r}")
    return value


@dataclass(frozen=True)
class FetchContext:
    """一次拉取的上下文。ingest_run_id 贯穿整条链路进入每一行数据。

    `watermark` 是可选的增量游标（阶段 F，core.ingest_watermark），含义由
    各适配器自己定义——可以是供应商的分页 token，也可以是"已处理到的最大
    trade_date"。不是每个适配器都要用它：不使用时行为等价于按
    partition_date 全量拉，这个字段只是提供一个可选的增量入口。
    """

    ingest_run_id: str
    partition_date: date
    dry_run: bool = False
    watermark: str | None = None


@dataclass(frozen=True)
class RawResponse:
    """原始响应。入库 provider_snapshot 之后才进入解析。

    `external_id` / `publish_time` / `checksum` 是阶段 F 补的三个可选字段，
    供需要在"原始响应"这一层（早于 NormalizedDocument 的归一化）就能对账
    与识别"这是不是同一份材料换了个标点重发"的适配器使用——不是每个适配器
    都要填：公告类来源用 NormalizedDocument.provider_doc_id /
    supersedes_provider_doc_id 已经能表达同一件事（见 DocumentWriter 阶段 F
    的更正路由），这三个字段主要服务尚无归一化模型的数据（如行情、事实）。
    """

    provider: str
    endpoint: str
    params: Mapping[str, Any]
    payload: Any
    http_status: int
    fetched_at: datetime
    cost_cents: Decimal | None = None
    external_id: str | None = None
    publish_time: datetime | None = None
    checksum: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.fetched_at, "fetched_at")
        if self.publish_time is not None:
            require_aware(self.publish_time, "publish_time")


@dataclass(frozen=True)
class FactRecord:
    """归一化的结构化事实。entity_ref 与 metric_field 仍是供应商侧标识，
    映射为 entity_id / metric_id 由中间件完成。"""

    entity_ref: str
    metric_field: str
    period: str
    period_end: date
    value: float
    unit: str
    currency: str | None
    valid_from: date
    known_at: datetime
    source_ref: str | None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        reject_future(self.known_at, "known_at")
        if self.period_end < self.valid_from:
            raise ValueError(f"period_end {self.period_end} 早于 valid_from {self.valid_from}")
        if self.known_at.date() < self.period_end:
            raise ValueError(
                f"known_at {self.known_at} 早于 period_end {self.period_end}"
                "——一份报告不可能在它描述的期间结束前就被知晓"
            )


@runtime_checkable
class Adapter(Protocol):
    """全部外部数据源的统一接口。"""

    provider: str

    def health(self) -> bool:
        """连通性与配额检查。Dagster 资产启动前调用。"""

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        """按分区拉取原始数据。必须幂等：同样的 ctx 产生同样的结果。"""

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        """计算该条记录的 known_at。规则见 docs/03-point-in-time.md §1.3。"""


@runtime_checkable
class FactAdapter(Adapter, Protocol):
    """结构化事实适配器。"""

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        """把原始响应解析成归一化事实。不写库。"""
