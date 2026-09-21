# P1a 数据接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让外部数据能按正确的时点语义进入数据库——适配器只负责拉取与归一化，入库统一走时点写入中间件，更正与幂等由数据库约束兜底。

**Architecture:** 三层。**适配器层**（`src/ragdemo/adapters/`）把各供应商的返回归一化成 `FactRecord` / `NormalizedDocument`，并各自实现 `known_at()`——只有它知道原始字段的语义。**中间件层**（`src/ragdemo/ingest/writer.py`）是全部入库路径的唯一闸门，负责幂等、更正事务、`superseded_at` 标记。**编排层**（Dagster 日分区资产）调度前两层，要求幂等可回填。

**Tech Stack:** Python 3.11+ / psycopg 3 / httpx / Dagster / pytest

**Spec:** [`docs/04-ingestion.md`](../../04-ingestion.md)、[`docs/03-point-in-time.md`](../../03-point-in-time.md)
**工作流：** [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W1.1–W1.7
**阶段验收：** [`docs/10-roadmap.md`](../../10-roadmap.md) P1（本计划覆盖其中的入库部分）
**前置：** [`2026-09-21-p0-foundation.md`](2026-09-21-p0-foundation.md) 全部 14 个任务已验收

## 对 P0 的接口假设

本计划在 P0 执行**之前**编写，以下 P0 产出被当作既成事实引用。P0 跑完若有出入，
受影响的任务要回改，并同步更新本文件：

| 假设的接口 | 来自 | 用在 |
|---|---|---|
| `temp_db` pytest fixture → 空库 DSN | P0 Task 2 | 全部数据库测试 |
| `ragdemo.db.migrate.migrate(conn, Path) -> list[str]` | P0 Task 3 | 全部数据库测试的建表 |
| `ragdemo.db.session.as_of_session(conn, as_of, *, tenant, user)` | P0 Task 10 | Task 3、Task 9 |
| `ragdemo.db.session.NaiveDatetimeError` | P0 Task 10 | Task 1 复用同一异常语义 |
| `core.fin_fact` 的 `fin_fact_live_uk` 部分唯一索引 | P0 Task 6 | Task 3 依赖它拦截漏打标记 |
| `core.provider_snapshot` 表 | P0 Task 6 | Task 4 |
| `core.entity_resolution_queue` 表 | P0 Task 8 | Task 9 |
| `pytest` 标记 `db` | P0 Task 1 | 全部数据库测试 |

## Global Constraints

- Python **3.11+**；完整类型注解；`ruff` 与 `mypy --strict` 必须通过。
- **TDD**：先写失败的测试，跑一遍确认它按预期失败，再写最小实现。
- **`known_at` 是「现实中最早可获知的时刻」，不是入库时间。** 入库时间叫 `ingested_at`，永远不得进入 `as_of` 过滤。不确定时取更晚的时刻。
- **业务代码不得 import 任何供应商 SDK**，只 import 本项目的适配器协议。
- **适配器不写库。** 拉取与归一化归适配器，入库归中间件。
- **Dagster 资产必须幂等**：同一分区重跑结果一致。
- **不用 `ON CONFLICT DO UPDATE`**：更正只能走 `superseded_at` + 插新行。
- **密钥只在环境变量或出网代理层**；`provider_snapshot.params` 入库前必须剥离认证字段。
- 重试耗尽后**让分区失败**，不得吞异常返回部分数据。
- 所有 `datetime` 必须带时区；naive datetime 一律在入口拒绝。
- 提交信息用 Conventional Commits。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `src/ragdemo/adapters/__init__.py` | 包声明 |
| `src/ragdemo/adapters/base.py` | `FetchContext` / `RawResponse` / `FactRecord` / `Adapter` / `FactAdapter` 协议 |
| `src/ragdemo/adapters/errors.py` | `AdapterError` / `RateLimited` / `QuotaExceeded` / `UpstreamUnavailable` |
| `src/ragdemo/adapters/http.py` | 限流 + 重试 + 配额的 HTTP 客户端 |
| `src/ragdemo/adapters/secrets.py` | 参数中的认证字段剥离 |
| `src/ragdemo/adapters/announcements.py` | `NormalizedBlock` / `NormalizedDocument` / `AnnouncementProvider` 协议 |
| `src/ragdemo/adapters/tushare.py` | Tushare 财务与行情适配器 |
| `src/ragdemo/adapters/edgar.py` | SEC EDGAR 适配器 |
| `src/ragdemo/adapters/mock/facts.py` | `MockFactAdapter`（一等公民，P1 管线靠它跑通） |
| `src/ragdemo/adapters/mock/announcements.py` | `MockAnnouncementProvider` |
| `src/ragdemo/ingest/snapshot.py` | `provider_snapshot` 落库 |
| `src/ragdemo/ingest/writer.py` | **时点写入中间件**（全部入库的唯一闸门） |
| `src/ragdemo/entities/resolver.py` | 实体解析三层降级 + 人工队列 |
| `src/ragdemo/ingest/assets.py` | Dagster 资产与日分区 |
| `src/ragdemo/ingest/definitions.py` | Dagster `Definitions` 与传感器 |
| `tests/contracts/adapter_contract.py` | 任何适配器都必须通过的契约测试基类 |
| `tests/fixtures/<provider>/` | 脱敏后的真实响应样本（录制回放） |

---

## Task 1: 适配器协议与错误类型

**Files:**
- Create: `src/ragdemo/adapters/__init__.py`, `src/ragdemo/adapters/base.py`, `src/ragdemo/adapters/errors.py`
- Test: `tests/adapters/__init__.py`, `tests/adapters/test_base.py`

**Interfaces:**
- Consumes: 无（P1a 的起点）
- Produces:
  - `FetchContext(ingest_run_id: str, partition_date: date, dry_run: bool = False)`
  - `RawResponse(provider, endpoint, params, payload, http_status, fetched_at, cost_cents=None)`
  - `FactRecord(entity_ref, metric_field, period, period_end, value, unit, currency, valid_from, known_at, source_ref)`
  - `Adapter` Protocol：`provider: str`、`health() -> bool`、`fetch(ctx, **params) -> Iterator[RawResponse]`、`known_at(record: Any) -> datetime`
  - `FactAdapter(Adapter)` Protocol：`parse(raw: RawResponse) -> Iterator[FactRecord]`
  - `require_aware(value: datetime, field: str) -> datetime`
  - `AdapterError` / `RateLimited(retry_after_s: float | None)` / `QuotaExceeded` / `UpstreamUnavailable`

- [ ] **Step 1: 写失败的测试**

`tests/adapters/__init__.py`：空文件。

`tests/adapters/test_base.py`：

```python
"""适配器协议的不变量：时区强制与不可变数据类。"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse, require_aware
from ragdemo.adapters.errors import AdapterError, RateLimited

UTC = timezone.utc


def test_require_aware_accepts_tz_aware() -> None:
    value = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    assert require_aware(value, "known_at") is value


def test_require_aware_rejects_naive() -> None:
    with pytest.raises(ValueError, match="known_at"):
        require_aware(datetime(2024, 10, 28, 18, 32), "known_at")


def test_fetch_context_is_frozen() -> None:
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    assert ctx.dry_run is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.ingest_run_id = "r2"  # type: ignore[misc]


def test_raw_response_is_frozen() -> None:
    raw = RawResponse(
        provider="mock", endpoint="/x", params={"a": 1}, payload=[],
        http_status=200, fetched_at=datetime.now(UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        raw.http_status = 500  # type: ignore[misc]


def test_fact_record_rejects_naive_known_at() -> None:
    with pytest.raises(ValueError, match="known_at"):
        FactRecord(
            entity_ref="688256.SH", metric_field="revenue", period="2024Q3",
            period_end=date(2024, 9, 30), value=1.0, unit="CNY", currency="CNY",
            valid_from=date(2024, 7, 1), known_at=datetime(2024, 10, 28),
            source_ref=None,
        )


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimited("too many requests", retry_after_s=2.5)
    assert isinstance(err, AdapterError)
    assert err.retry_after_s == 2.5


def test_known_at_must_not_be_in_the_future() -> None:
    """未来的 known_at 意味着「还没发生就知道了」，是最严重的时点缺陷。"""
    future = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(ValueError, match="未来"):
        FactRecord(
            entity_ref="688256.SH", metric_field="revenue", period="2024Q3",
            period_end=date(2024, 9, 30), value=1.0, unit="CNY", currency="CNY",
            valid_from=date(2024, 7, 1), known_at=future, source_ref=None,
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/adapters/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/adapters/__init__.py`：

```python
"""外部数据源适配器。业务代码只 import 本包的协议，不 import 供应商 SDK。"""
```

`src/ragdemo/adapters/errors.py`：

```python
"""适配器错误类型。"""
from __future__ import annotations


class AdapterError(RuntimeError):
    """适配器层的基类错误。"""


class RateLimited(AdapterError):
    """被上游限速。retry_after_s 来自 Retry-After 响应头，没有则为 None。"""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QuotaExceeded(AdapterError):
    """超出本日或本月配额。不重试——重试只会继续烧钱。"""


class UpstreamUnavailable(AdapterError):
    """重试耗尽后仍失败。抛出它会让 Dagster 分区失败，这是刻意的：
    部分数据入库比没数据更糟，下游会以为数据完整。"""
```

`src/ragdemo/adapters/base.py`：

```python
"""适配器协议与归一化数据结构。

设计要点：known_at() 是适配器的方法而不是中间件的逻辑。每个数据源的
「现实中最早可获知的时刻」规则不同——公告用发布时间、财务用公告日、
行情用收盘时刻——只有适配器知道原始字段的语义。
见 docs/03-point-in-time.md §1.3。
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


def require_aware(value: datetime, field_name: str) -> datetime:
    """强制 datetime 带时区。naive datetime 的含义随服务器时区变化，必须在入口拒绝。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须带时区，收到 naive datetime: {value!r}")
    return value


def reject_future(value: datetime, field_name: str) -> datetime:
    """known_at 落在未来意味着「事情还没发生我们就知道了」。"""
    if value > datetime.now(timezone.utc):
        raise ValueError(f"{field_name} 落在未来: {value!r}")
    return value


@dataclass(frozen=True)
class FetchContext:
    """一次拉取的上下文。ingest_run_id 贯穿整条链路进入每一行数据。"""

    ingest_run_id: str
    partition_date: date
    dry_run: bool = False


@dataclass(frozen=True)
class RawResponse:
    """原始响应。入库 provider_snapshot 之后才进入解析。"""

    provider: str
    endpoint: str
    params: Mapping[str, Any]
    payload: Any
    http_status: int
    fetched_at: datetime
    cost_cents: Decimal | None = None

    def __post_init__(self) -> None:
        require_aware(self.fetched_at, "fetched_at")


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


@runtime_checkable
class Adapter(Protocol):
    """全部外部数据源的统一接口。"""

    provider: str

    def health(self) -> bool:
        """连通性与配额检查。Dagster 资产启动前调用。"""

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:
        """按分区拉取原始数据。必须幂等：同样的 ctx 产生同样的结果。"""

    def known_at(self, record: Any) -> datetime:
        """计算该条记录的 known_at。规则见 docs/03-point-in-time.md §1.3。"""


@runtime_checkable
class FactAdapter(Adapter, Protocol):
    """结构化事实适配器。"""

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        """把原始响应解析成归一化事实。不写库。"""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/adapters/test_base.py -v && make lint && make typecheck`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters tests/adapters
git commit -m "feat(adapters): 适配器协议与错误类型，入口强制时区与非未来 known_at"
```

---

## Task 2: 契约测试基类与 Mock 事实适配器

Mock 不是测试脚手架，是**一等公民**——公告供应商选定前，整条 P1 管线靠它跑通
（[adr/0005](../../adr/0005-announcement-provider-abstraction.md)）。

**Files:**
- Create: `src/ragdemo/adapters/mock/__init__.py`, `src/ragdemo/adapters/mock/facts.py`, `tests/contracts/__init__.py`, `tests/contracts/adapter_contract.py`, `tests/contracts/test_mock_facts.py`

**Interfaces:**
- Consumes: Task 1 的全部协议
- Produces:
  - `MockFactAdapter(records: Sequence[FactRecord] | None = None, *, provider: str = "mock")`，属性 `call_count: int`
  - `AdapterContract`（pytest 基类）：子类实现 `make_adapter() -> Adapter` 与 `make_context(partition_date) -> FetchContext`，自动获得 5 条契约测试

- [ ] **Step 1: 写失败的测试**

`tests/contracts/__init__.py`：空文件。

`tests/contracts/adapter_contract.py`：

```python
"""任何 Adapter 实现都必须通过的契约。

用法：子类化 AdapterContract，实现 make_adapter()，即自动获得这 5 条测试。
真实适配器与 Mock 走同一套契约——这是 Mock 能替代真实数据推进 P1 的前提。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ragdemo.adapters.base import Adapter, FetchContext

UTC = timezone.utc


class AdapterContract:
    """契约测试基类。不要在这里加 pytest fixture，子类可能有自己的。"""

    def make_adapter(self) -> Adapter:
        raise NotImplementedError

    def make_context(self, partition_date: date) -> FetchContext:
        return FetchContext(ingest_run_id="contract-run", partition_date=partition_date)

    def test_provider_is_a_nonempty_string(self) -> None:
        assert isinstance(self.make_adapter().provider, str)
        assert self.make_adapter().provider != ""

    def test_health_returns_bool(self) -> None:
        assert isinstance(self.make_adapter().health(), bool)

    def test_known_at_is_timezone_aware_and_not_future(self) -> None:
        adapter = self.make_adapter()
        ctx = self.make_context(date(2024, 10, 28))
        now = datetime.now(UTC)
        for raw in adapter.fetch(ctx):
            for item in _iter_records(raw.payload):
                ka = adapter.known_at(item)
                assert ka.tzinfo is not None and ka.utcoffset() is not None
                assert ka <= now

    def test_fetch_is_idempotent(self) -> None:
        """同一 FetchContext 两次调用必须给出相同结果，否则回填不可复现。"""
        adapter = self.make_adapter()
        ctx = self.make_context(date(2024, 10, 28))
        first = [r.payload for r in adapter.fetch(ctx)]
        second = [r.payload for r in adapter.fetch(ctx)]
        assert first == second

    def test_backfill_known_at_stays_historical(self) -> None:
        """回填两年前的分区时，known_at 必须落在那个历史区间，
        不能是回填当天——否则历史数据在回测中整体消失。
        见 docs/03-point-in-time.md §1.2。
        """
        adapter = self.make_adapter()
        old = date.today() - timedelta(days=730)
        for raw in adapter.fetch(self.make_context(old)):
            for item in _iter_records(raw.payload):
                assert adapter.known_at(item).date() <= old + timedelta(days=90)


def _iter_records(payload: object) -> list[object]:
    return list(payload) if isinstance(payload, list) else [payload]
```

`tests/contracts/test_mock_facts.py`：

```python
"""MockFactAdapter 必须通过全部适配器契约，并支持注入自定义记录。"""
from __future__ import annotations

from datetime import date

from ragdemo.adapters.base import Adapter, FetchContext
from ragdemo.adapters.mock.facts import MockFactAdapter
from tests.contracts.adapter_contract import AdapterContract


class TestMockFactAdapterContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return MockFactAdapter()


def test_parse_yields_fact_records() -> None:
    adapter = MockFactAdapter()
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    records = [rec for raw in adapter.fetch(ctx) for rec in adapter.parse(raw)]
    assert records
    assert all(r.known_at.tzinfo is not None for r in records)


def test_call_count_tracks_fetches() -> None:
    adapter = MockFactAdapter()
    ctx = FetchContext(ingest_run_id="r1", partition_date=date(2024, 10, 28))
    list(adapter.fetch(ctx))
    list(adapter.fetch(ctx))
    assert adapter.call_count == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/contracts -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters.mock'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/adapters/mock/__init__.py`：

```python
"""Mock 适配器。它们是一等公民：公告供应商选定前，P1 管线靠它们跑通。"""
```

`src/ragdemo/adapters/mock/facts.py`：

```python
"""Mock 事实适配器。

默认数据取自真实三季报的公开数字，确保 P1 的评测不是在理想化数据上跑出来的。
known_at 一律由 partition_date 推导，因此回填历史分区时天然落在历史区间。
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse

UTC = timezone.utc

_DEFAULT_ROWS: tuple[dict[str, Any], ...] = (
    {"ts_code": "688256.SH", "field": "revenue_total", "period": "2024Q3",
     "period_end": "2024-09-30", "value": 12340.5, "unit": "CNY"},
    {"ts_code": "688256.SH", "field": "rd_expense", "period": "2024Q3",
     "period_end": "2024-09-30", "value": 1890.2, "unit": "CNY"},
    {"ts_code": "002049.SZ", "field": "revenue_total", "period": "2024Q3",
     "period_end": "2024-09-30", "value": 4021.8, "unit": "CNY"},
)


class MockFactAdapter:
    """可注入记录的事实适配器。"""

    def __init__(
        self,
        records: Sequence[dict[str, Any]] | None = None,
        *,
        provider: str = "mock",
    ) -> None:
        self.provider = provider
        self._rows = tuple(records) if records is not None else _DEFAULT_ROWS
        self.call_count = 0

    def health(self) -> bool:
        return True

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:
        self.call_count += 1
        yield RawResponse(
            provider=self.provider,
            endpoint="/mock/facts",
            params={"partition": ctx.partition_date.isoformat(), **params},
            payload=[dict(r, ann_date=ctx.partition_date.isoformat()) for r in self._rows],
            http_status=200,
            fetched_at=datetime.now(UTC),
        )

    def known_at(self, record: Any) -> datetime:
        """公告日当日 23:59:59 —— 只给日期不给时间时的保守取法。"""
        ann = date.fromisoformat(str(record["ann_date"]))
        return datetime.combine(ann, time(23, 59, 59), tzinfo=UTC)

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        for row in raw.payload:
            period_end = date.fromisoformat(str(row["period_end"]))
            yield FactRecord(
                entity_ref=str(row["ts_code"]),
                metric_field=str(row["field"]),
                period=str(row["period"]),
                period_end=period_end,
                value=float(row["value"]),
                unit=str(row["unit"]),
                currency="CNY",
                valid_from=period_end - timedelta(days=89),
                known_at=self.known_at(row),
                source_ref=f"{row['ts_code']}:{row['period']}:{row['field']}",
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/contracts -v && make typecheck`
Expected: 7 passed（5 条契约 + 2 条专属）

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters/mock tests/contracts
git commit -m "feat(adapters): 契约测试基类与 Mock 事实适配器"
```

---

## Task 3: 时点写入中间件

**全部入库路径的唯一闸门。** 在它定稿前不要并行写任何适配器的入库逻辑
（[`docs/11-sdlc.md`](../../11-sdlc.md) §5.4）。

**Files:**
- Create: `src/ragdemo/ingest/__init__.py`, `src/ragdemo/ingest/writer.py`, `tests/ingest/__init__.py`, `tests/ingest/test_writer.py`

**Interfaces:**
- Consumes: Task 1 的 `FactRecord`；P0 的 `core.fin_fact` 与 `fin_fact_live_uk`
- Produces:
  - `WriteOutcome` 枚举：`INSERTED` / `SKIPPED_IDENTICAL` / `CORRECTED`
  - `OutOfOrderCorrection(RuntimeError)`
  - `UnknownEntityRef(RuntimeError)` / `UnknownMetricField(RuntimeError)`
  - `PointInTimeWriter(conn, *, ingest_run_id: str, source: str)`
    - `write_fact(record: FactRecord) -> WriteOutcome`
    - `write_facts(records: Iterable[FactRecord]) -> dict[WriteOutcome, int]`

- [ ] **Step 1: 写失败的测试**

`tests/ingest/__init__.py`：空文件。

`tests/ingest/test_writer.py`：

```python
"""时点写入中间件：幂等、更正、乱序拒绝。

这组测试是 P1 正确性的核心。写错了，历史库会被污染，而历史库按设计不可删。
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import FactRecord
from ragdemo.db.migrate import migrate
from ragdemo.ingest.writer import (
    OutOfOrderCorrection,
    PointInTimeWriter,
    UnknownEntityRef,
    WriteOutcome,
)

MIGRATIONS = Path("db/migrations")


def _record(value: float, known_at: datetime, ref: str = "688256.SH") -> FactRecord:
    return FactRecord(
        entity_ref=ref, metric_field="revenue_total", period="2024Q3",
        period_end=date(2024, 9, 30), value=value, unit="CNY", currency="CNY",
        valid_from=date(2024, 7, 1), known_at=known_at, source_ref="x",
    )


@pytest.fixture()
def writer(temp_db: str) -> PointInTimeWriter:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.execute(
        "INSERT INTO core.node_metric (metric_id, metric_name, metric_role,"
        " frequency, source_type, definition, unit) "
        "VALUES ('revenue_total','营业收入','confirming','quarterly','filing','合并口径','CNY')"
    )
    conn.execute(
        "INSERT INTO core.metric_source_map (metric_id, provider, provider_field) "
        "VALUES ('revenue_total','tushare','revenue_total')"
    )
    conn.commit()
    return PointInTimeWriter(conn, ingest_run_id="r1", source="tushare")


@pytest.mark.db
def test_first_write_inserts(writer: PointInTimeWriter) -> None:
    outcome = writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    assert outcome is WriteOutcome.INSERTED


@pytest.mark.db
def test_identical_rewrite_is_skipped_not_duplicated(writer: PointInTimeWriter) -> None:
    """幂等：Dagster 重跑同一分区不得产生第二行。"""
    rec = _record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC))
    assert writer.write_fact(rec) is WriteOutcome.INSERTED
    assert writer.write_fact(rec) is WriteOutcome.SKIPPED_IDENTICAL
    (n,) = writer.conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_changed_value_triggers_correction(writer: PointInTimeWriter) -> None:
    """更正 = 给旧行打 superseded_at + 插新行，两行共存。"""
    writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    corrected_at = datetime(2025, 1, 15, 19, 0, tzinfo=UTC)
    assert writer.write_fact(_record(12500.0, corrected_at)) is WriteOutcome.CORRECTED

    rows = writer.conn.execute(
        "SELECT value, superseded_at FROM core.fin_fact ORDER BY known_at"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == corrected_at, "旧行的 superseded_at 必须等于新行的 known_at"
    assert rows[1][1] is None


@pytest.mark.db
def test_older_known_at_is_rejected(writer: PointInTimeWriter) -> None:
    """乱序到达：不能用更早可知的数据去覆盖更晚可知的数据。"""
    writer.write_fact(_record(12500.0, datetime(2025, 1, 15, 19, 0, tzinfo=UTC)))
    with pytest.raises(OutOfOrderCorrection):
        writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))


@pytest.mark.db
def test_unknown_entity_ref_raises_and_writes_nothing(writer: PointInTimeWriter) -> None:
    with pytest.raises(UnknownEntityRef):
        writer.write_fact(_record(1.0, datetime(2024, 10, 28, 18, 32, tzinfo=UTC), ref="999999.SH"))
    (n,) = writer.conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert n == 0


@pytest.mark.db
def test_ingested_at_differs_from_known_at(writer: PointInTimeWriter) -> None:
    """known_at 来自数据本身，ingested_at 是 now()。两者混同会毁掉回填。"""
    writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    known_at, ingested_at = writer.conn.execute(
        "SELECT known_at, ingested_at FROM core.fin_fact"
    ).fetchone()  # type: ignore[misc]
    assert known_at < ingested_at


@pytest.mark.db
def test_write_facts_reports_counts_per_outcome(writer: PointInTimeWriter) -> None:
    t = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    counts = writer.write_facts([_record(12340.5, t), _record(12340.5, t)])
    assert counts == {WriteOutcome.INSERTED: 1, WriteOutcome.SKIPPED_IDENTICAL: 1}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/ingest/test_writer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.ingest'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/ingest/__init__.py`：

```python
"""入库层。时点写入中间件是全部事实与文档进入数据库的唯一路径。"""
```

`src/ragdemo/ingest/writer.py`：

```python
"""时点写入中间件。

三条不可妥协的规则（docs/03-point-in-time.md §3）：
1. 更正是追加，不是更新——给旧行打 superseded_at，再插新行；
2. superseded_at 等于新行的 known_at，不是 now()，这样任意 as_of 恰好命中一行；
3. known_at 来自数据本身（适配器计算），ingested_at 才是 now()。

数据库的 fin_fact_live_uk 部分唯一索引是最后一道防线：忘记第 1 步会直接冲突失败。
"""
from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

import psycopg

from ragdemo.adapters.base import FactRecord

_VALUE_EPSILON = 1e-9


class WriteOutcome(Enum):
    INSERTED = "inserted"
    SKIPPED_IDENTICAL = "skipped_identical"
    CORRECTED = "corrected"


class OutOfOrderCorrection(RuntimeError):
    """新数据的 known_at 早于当前有效行的 known_at。"""


class UnknownEntityRef(RuntimeError):
    """供应商代码无法解析为 entity_id。"""


class UnknownMetricField(RuntimeError):
    """供应商字段名无法映射为 metric_id。"""


class PointInTimeWriter:
    """把归一化记录写进时点表。"""

    def __init__(self, conn: psycopg.Connection, *, ingest_run_id: str, source: str) -> None:
        self.conn = conn
        self.ingest_run_id = ingest_run_id
        self.source = source
        self._entity_cache: dict[str, str] = {}
        self._metric_cache: dict[str, str] = {}

    # --- 标识映射 ---------------------------------------------------------

    def _entity_id(self, entity_ref: str) -> str:
        if entity_ref not in self._entity_cache:
            row = self.conn.execute(
                "SELECT entity_id FROM core.entity WHERE tushare_code = %s"
                " OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
                (entity_ref, entity_ref, entity_ref, entity_ref),
            ).fetchone()
            if row is None:
                raise UnknownEntityRef(f"无法解析供应商代码 {entity_ref!r} 为 entity_id")
            self._entity_cache[entity_ref] = str(row[0])
        return self._entity_cache[entity_ref]

    def _metric_id(self, metric_field: str) -> str:
        key = f"{self.source}:{metric_field}"
        if key not in self._metric_cache:
            row = self.conn.execute(
                "SELECT metric_id FROM core.metric_source_map "
                " WHERE provider = %s AND provider_field = %s ORDER BY priority LIMIT 1",
                (self.source, metric_field),
            ).fetchone()
            if row is None:
                raise UnknownMetricField(
                    f"{self.source} 的字段 {metric_field!r} 未在 metric_source_map 中登记"
                )
            self._metric_cache[key] = str(row[0])
        return self._metric_cache[key]

    # --- 写入 -------------------------------------------------------------

    def write_fact(self, record: FactRecord) -> WriteOutcome:
        """写一条事实。已存在相同值则跳过，值不同则走更正流程。"""
        entity_id = self._entity_id(record.entity_ref)
        metric_id = self._metric_id(record.metric_field)

        with self.conn.transaction():
            live = self.conn.execute(
                "SELECT fact_id, value, known_at FROM core.fin_fact "
                " WHERE entity_id = %s AND metric_id = %s AND period = %s"
                "   AND superseded_at IS NULL FOR UPDATE",
                (entity_id, metric_id, record.period),
            ).fetchone()

            if live is not None:
                _, existing_value, existing_known_at = live
                if record.known_at < existing_known_at:
                    raise OutOfOrderCorrection(
                        f"{entity_id}/{metric_id}/{record.period}: 新数据 known_at "
                        f"{record.known_at} 早于现有 {existing_known_at}"
                    )
                if abs(float(existing_value) - record.value) < _VALUE_EPSILON:
                    return WriteOutcome.SKIPPED_IDENTICAL
                self.conn.execute(
                    "UPDATE core.fin_fact SET superseded_at = %s "
                    " WHERE entity_id = %s AND metric_id = %s AND period = %s"
                    "   AND superseded_at IS NULL",
                    (record.known_at, entity_id, metric_id, record.period),
                )
                outcome = WriteOutcome.CORRECTED
            else:
                outcome = WriteOutcome.INSERTED

            self.conn.execute(
                "INSERT INTO core.fin_fact (entity_id, metric_id, period, period_end,"
                " value, unit, currency, valid_from, known_at, source, source_ref,"
                " ingest_run_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    entity_id, metric_id, record.period, record.period_end,
                    record.value, record.unit, record.currency, record.valid_from,
                    record.known_at, self.source, record.source_ref, self.ingest_run_id,
                ),
            )
        return outcome

    def write_facts(self, records: Iterable[FactRecord]) -> dict[WriteOutcome, int]:
        counts: dict[WriteOutcome, int] = {}
        for record in records:
            outcome = self.write_fact(record)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/ingest/test_writer.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/ingest tests/ingest
git commit -m "feat(ingest): 时点写入中间件，幂等写入与追加式更正"
```

---

## Task 4: 原始响应留存与密钥剥离

**Files:**
- Create: `src/ragdemo/adapters/secrets.py`, `src/ragdemo/ingest/snapshot.py`, `tests/adapters/test_secrets.py`, `tests/ingest/test_snapshot.py`

**Interfaces:**
- Consumes: Task 1 的 `RawResponse`
- Produces:
  - `SECRET_PARAM_NAMES: frozenset[str]`
  - `strip_secrets(params: Mapping[str, Any]) -> dict[str, Any]`
  - `save_snapshot(conn, raw: RawResponse, *, ingest_run_id: str) -> int`（返回 `snapshot_id`）
  - `MAX_INLINE_PAYLOAD_BYTES: int = 1_000_000`

- [ ] **Step 1: 写失败的测试**

`tests/adapters/test_secrets.py`：

```python
"""密钥剥离：provider_snapshot 会原样记录调用参数，token 混在里面就进了数据库。"""
from __future__ import annotations

from ragdemo.adapters.secrets import strip_secrets


def test_known_secret_keys_are_redacted() -> None:
    out = strip_secrets({"token": "abc", "ts_code": "688256.SH"})
    assert out == {"token": "[REDACTED]", "ts_code": "688256.SH"}


def test_matching_is_case_insensitive_and_substring_aware() -> None:
    out = strip_secrets({"API_Key": "k", "access_token": "t", "X-Auth-Secret": "s"})
    assert set(out.values()) == {"[REDACTED]"}


def test_nested_dicts_are_walked() -> None:
    out = strip_secrets({"headers": {"Authorization": "Bearer x"}, "q": "算力"})
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["q"] == "算力"


def test_non_secret_values_are_untouched_and_input_not_mutated() -> None:
    src = {"ts_code": "688256.SH", "limit": 100}
    out = strip_secrets(src)
    assert out == src
    assert out is not src
```

`tests/ingest/test_snapshot.py`：

```python
"""原始响应留存：永不修改、永不删除，且不含密钥。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import RawResponse
from ragdemo.db.migrate import migrate
from ragdemo.ingest.snapshot import MAX_INLINE_PAYLOAD_BYTES, save_snapshot

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


def _raw(payload: object, params: dict[str, object] | None = None) -> RawResponse:
    return RawResponse(
        provider="tushare", endpoint="/income", params=params or {"token": "sk-secret"},
        payload=payload, http_status=200, fetched_at=datetime.now(UTC),
    )


@pytest.mark.db
def test_snapshot_stores_payload_and_redacts_params(conn: psycopg.Connection) -> None:
    sid = save_snapshot(conn, _raw([{"a": 1}]), ingest_run_id="r1")
    params, response = conn.execute(
        "SELECT params, response FROM core.provider_snapshot WHERE snapshot_id = %s", (sid,)
    ).fetchone()  # type: ignore[misc]
    assert params["token"] == "[REDACTED]"
    assert response == [{"a": 1}]


@pytest.mark.db
def test_oversized_payload_goes_to_response_ref(conn: psycopg.Connection) -> None:
    """超过 1MB 的响应不进 jsonb 列，避免单表膨胀。"""
    big = [{"x": "y" * 1000} for _ in range(2000)]
    sid = save_snapshot(conn, _raw(big), ingest_run_id="r1")
    response, ref = conn.execute(
        "SELECT response, response_ref FROM core.provider_snapshot WHERE snapshot_id = %s",
        (sid,),
    ).fetchone()  # type: ignore[misc]
    assert response is None
    assert ref is not None and str(MAX_INLINE_PAYLOAD_BYTES) not in ref
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/adapters/test_secrets.py tests/ingest/test_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters.secrets'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/adapters/secrets.py`：

```python
"""参数中的认证字段剥离。

provider_snapshot.params 会原样记录调用参数。某些供应商把 token 放在
query string 里，不剥离就等于把密钥写进数据库（docs/09-compliance-security.md §4.2）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

SECRET_PARAM_NAMES = frozenset(
    {
        "token", "api_key", "apikey", "access_token", "refresh_token",
        "secret", "client_secret", "password", "passwd", "pwd",
        "authorization", "auth", "sig", "signature", "credential",
    }
)


def _is_secret(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(name in lowered for name in SECRET_PARAM_NAMES)


def strip_secrets(params: Mapping[str, Any]) -> dict[str, Any]:
    """返回一份剥离了认证字段的副本。输入不被修改。"""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if _is_secret(str(key)):
            out[key] = REDACTED
        elif isinstance(value, Mapping):
            out[key] = strip_secrets(value)
        else:
            out[key] = value
    return out
```

`src/ragdemo/ingest/snapshot.py`：

```python
"""原始响应留存。

任何「数据对不对」的争议都回到 provider_snapshot 重放。因此它永不修改、永不删除。
"""
from __future__ import annotations

import json
import uuid

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.secrets import strip_secrets

MAX_INLINE_PAYLOAD_BYTES = 1_000_000


def save_snapshot(conn: psycopg.Connection, raw: RawResponse, *, ingest_run_id: str) -> int:
    """落库一条原始响应，返回 snapshot_id。

    超过 MAX_INLINE_PAYLOAD_BYTES 的响应不写 jsonb 列，只记一个对象存储 key；
    实际上传由调用方完成（P1 阶段对象存储尚未接入，key 先占位）。
    """
    encoded = json.dumps(raw.payload, ensure_ascii=False, default=str)
    oversized = len(encoded.encode("utf-8")) > MAX_INLINE_PAYLOAD_BYTES
    response_ref = f"snapshot/{raw.provider}/{uuid.uuid4().hex}.json" if oversized else None

    row = conn.execute(
        "INSERT INTO core.provider_snapshot (provider, endpoint, params, response,"
        " response_ref, http_status, ingest_run_id, cost_cents) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING snapshot_id",
        (
            raw.provider,
            raw.endpoint,
            Jsonb(strip_secrets(raw.params)),
            None if oversized else Jsonb(raw.payload),
            response_ref,
            raw.http_status,
            ingest_run_id,
            raw.cost_cents,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/adapters/test_secrets.py tests/ingest/test_snapshot.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters/secrets.py src/ragdemo/ingest/snapshot.py tests/adapters/test_secrets.py tests/ingest/test_snapshot.py
git commit -m "feat(ingest): 原始响应留存与参数密钥剥离"
```

---

## Task 5: 限流、重试与配额的 HTTP 客户端

**Files:**
- Create: `src/ragdemo/adapters/http.py`, `config/providers.yaml`, `tests/adapters/test_http.py`
- Modify: `pyproject.toml`（加 `httpx`、`pyyaml`）

**Interfaces:**
- Consumes: Task 1 的 `RawResponse` 与错误类型
- Produces:
  - `RetryPolicy(max_attempts=4, backoff_base_s=1.0, retry_on_status=frozenset({429,500,502,503,504}))`
  - `TokenBucket(rate_per_minute: int)`，方法 `acquire() -> float`（返回需等待的秒数）
  - `ProviderConfig`（从 `config/providers.yaml` 加载）
  - `HttpClient(provider, base_url, *, policy, bucket, transport=None)`，方法 `get_json(endpoint, params) -> RawResponse`

- [ ] **Step 1: 写失败的测试**

`tests/adapters/test_http.py`：

```python
"""HTTP 客户端：退避、Retry-After、重试耗尽后失败而非返回部分数据。"""
from __future__ import annotations

import httpx
import pytest

from ragdemo.adapters.errors import RateLimited, UpstreamUnavailable
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket


def _client(handler: httpx.MockTransport, **kw: object) -> HttpClient:
    return HttpClient(
        provider="test", base_url="https://example.test",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=handler, **kw,  # type: ignore[arg-type]
    )


def test_successful_request_returns_raw_response() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    raw = _client(transport).get_json("/x", {"a": 1})
    assert raw.http_status == 200
    assert raw.payload == {"ok": True}
    assert raw.fetched_at.tzinfo is not None


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    raw = _client(httpx.MockTransport(handler)).get_json("/x", {})
    assert raw.payload == {"ok": True}
    assert calls["n"] == 3


def test_exhausted_retries_raise_instead_of_returning_partial() -> None:
    """部分数据入库比没数据更糟——下游会以为数据完整。"""
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(UpstreamUnavailable):
        _client(transport).get_json("/x", {})


def test_429_surfaces_retry_after() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "7"})
    )
    with pytest.raises(UpstreamUnavailable) as exc:
        _client(transport).get_json("/x", {})
    assert isinstance(exc.value.__cause__, RateLimited)
    assert exc.value.__cause__.retry_after_s == 7.0


def test_4xx_other_than_429_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(UpstreamUnavailable):
        _client(httpx.MockTransport(handler)).get_json("/x", {})
    assert calls["n"] == 1, "404 不该重试"


def test_token_bucket_limits_rate() -> None:
    bucket = TokenBucket(rate_per_minute=60)
    assert bucket.acquire() == 0.0
    assert bucket.acquire() > 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/adapters/test_http.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters.http'`

- [ ] **Step 3: 写最小实现**

`pyproject.toml` 的 `dependencies` 改为（新增最后两项）：

```toml
dependencies = [
    "psycopg[binary]>=3.2",
    "click>=8.1",
    "httpx>=0.27",
    "pyyaml>=6.0",
]
```

`config/providers.yaml`：

```yaml
# 每个供应商的限流、重试与配额。密钥不在这里——它只在出网代理层。
tushare:
  base_url: https://api.tushare.pro
  rate_limit_qpm: 200
  daily_quota: 50000
  timeout_s: 30
  retry:
    max_attempts: 4
    backoff_base_s: 1.0
  cost_per_call_cents: 0

edgar:
  base_url: https://data.sec.gov
  rate_limit_qpm: 540        # SEC 要求 ≤ 10 req/s
  daily_quota: 200000
  timeout_s: 30
  retry:
    max_attempts: 4
    backoff_base_s: 1.0
  cost_per_call_cents: 0
```

`src/ragdemo/adapters/http.py`：

```python
"""带限流、重试与配额的 HTTP 客户端。

只重试幂等的读操作。429 优先读 Retry-After，没有才用指数退避。
重试耗尽抛 UpstreamUnavailable —— 让 Dagster 分区失败，而不是返回部分数据。
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.errors import QuotaExceeded, RateLimited, UpstreamUnavailable

UTC = timezone.utc
DEFAULT_CONFIG_PATH = Path("config/providers.yaml")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_base_s: float = 1.0
    retry_on_status: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    rate_limit_qpm: int
    daily_quota: int
    timeout_s: float
    retry: RetryPolicy
    cost_per_call_cents: Decimal

    @staticmethod
    def load(provider: str, path: Path = DEFAULT_CONFIG_PATH) -> ProviderConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))[provider]
        retry = raw.get("retry", {})
        return ProviderConfig(
            base_url=raw["base_url"],
            rate_limit_qpm=int(raw["rate_limit_qpm"]),
            daily_quota=int(raw["daily_quota"]),
            timeout_s=float(raw.get("timeout_s", 30)),
            retry=RetryPolicy(
                max_attempts=int(retry.get("max_attempts", 4)),
                backoff_base_s=float(retry.get("backoff_base_s", 1.0)),
            ),
            cost_per_call_cents=Decimal(str(raw.get("cost_per_call_cents", 0))),
        )


@dataclass
class TokenBucket:
    """每分钟 rate_per_minute 个令牌的漏桶。acquire() 返回需要等待的秒数。"""

    rate_per_minute: int
    _allowance: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        self._allowance = float(self.rate_per_minute)
        self._last = time.monotonic()

    def acquire(self) -> float:
        now = time.monotonic()
        self._allowance = min(
            float(self.rate_per_minute),
            self._allowance + (now - self._last) * self.rate_per_minute / 60.0,
        )
        self._last = now
        if self._allowance >= 1.0:
            self._allowance -= 1.0
            return 0.0
        return (1.0 - self._allowance) * 60.0 / self.rate_per_minute


class HttpClient:
    """一个供应商一个实例。密钥由出网代理注入，这里不持有任何凭据。"""

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        policy: RetryPolicy,
        bucket: TokenBucket,
        timeout_s: float = 30.0,
        daily_quota: int | None = None,
        cost_per_call_cents: Decimal = Decimal(0),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.bucket = bucket
        self.daily_quota = daily_quota
        self.cost_per_call_cents = cost_per_call_cents
        self.calls_today = 0
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout_s, transport=transport
        )

    def get_json(self, endpoint: str, params: Mapping[str, Any]) -> RawResponse:
        if self.daily_quota is not None and self.calls_today >= self.daily_quota:
            raise QuotaExceeded(f"{self.provider} 已达每日配额 {self.daily_quota}")

        last_error: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            wait = self.bucket.acquire()
            if wait > 0:
                time.sleep(wait)

            try:
                response = self._client.get(endpoint, params=dict(params))
            except httpx.TransportError as exc:
                last_error = exc
            else:
                self.calls_today += 1
                if response.status_code < 400:
                    return RawResponse(
                        provider=self.provider,
                        endpoint=endpoint,
                        params=params,
                        payload=response.json(),
                        http_status=response.status_code,
                        fetched_at=datetime.now(UTC),
                        cost_cents=self.cost_per_call_cents,
                    )
                if response.status_code not in self.policy.retry_on_status:
                    raise UpstreamUnavailable(
                        f"{self.provider} {endpoint} 返回 {response.status_code}（不重试）"
                    )
                last_error = _error_for(response)

            if attempt < self.policy.max_attempts:
                time.sleep(_backoff_seconds(last_error, attempt, self.policy))

        raise UpstreamUnavailable(
            f"{self.provider} {endpoint} 重试 {self.policy.max_attempts} 次后仍失败"
        ) from last_error


def _error_for(response: httpx.Response) -> Exception:
    if response.status_code == 429:
        header = response.headers.get("Retry-After")
        return RateLimited(
            "上游限速", retry_after_s=float(header) if header else None
        )
    return UpstreamUnavailable(f"HTTP {response.status_code}")


def _backoff_seconds(error: Exception | None, attempt: int, policy: RetryPolicy) -> float:
    if isinstance(error, RateLimited) and error.retry_after_s is not None:
        return error.retry_after_s
    return policy.backoff_base_s * (2 ** (attempt - 1))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pip install -e ".[dev]" && pytest tests/adapters/test_http.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters/http.py config/providers.yaml tests/adapters/test_http.py pyproject.toml
git commit -m "feat(adapters): 限流重试配额的 HTTP 客户端"
```

---

## Task 6: Tushare 适配器

**Files:**
- Create: `src/ragdemo/adapters/tushare.py`, `tests/fixtures/tushare/income_2024q3.json`, `tests/fixtures/tushare/daily_20241028.json`, `tests/adapters/test_tushare.py`

**Interfaces:**
- Consumes: Task 1 协议、Task 5 的 `HttpClient`
- Produces:
  - `TushareAdapter(client: HttpClient)`，`provider = "tushare"`
  - `parse(raw) -> Iterator[FactRecord]`
  - `parse_prices(raw) -> Iterator[PriceRow]`
  - `PriceRow(entity_ref, trade_date, open, high, low, close, pre_close, volume, amount, adj_factor, is_suspended, known_at)`
  - `A_SHARE_CLOSE_LOCAL = time(15, 30)`、`CST = timezone(timedelta(hours=8))`

- [ ] **Step 1: 写失败的测试**

`tests/fixtures/tushare/income_2024q3.json`（脱敏样本，字段名取自 Tushare `income` 接口）：

```json
{"code": 0, "msg": null, "data": {
  "fields": ["ts_code", "ann_date", "f_ann_date", "end_date", "revenue", "rd_exp"],
  "items": [
    ["688256.SH", "20241028", "20241028", "20240930", 1234050000.0, 189020000.0],
    ["002049.SZ", "20241025", "20241025", "20240930", 402180000.0, 51200000.0]
  ]}}
```

`tests/fixtures/tushare/daily_20241028.json`：

```json
{"code": 0, "msg": null, "data": {
  "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"],
  "items": [
    ["688256.SH", "20241028", 512.0, 528.8, 509.1, 524.3, 510.0, 42150.0, 2189000.0]
  ]}}
```

`tests/adapters/test_tushare.py`：

```python
"""Tushare 适配器：known_at 必须取公告日，不是期末日。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ragdemo.adapters.base import Adapter, RawResponse
from ragdemo.adapters.tushare import TushareAdapter
from tests.contracts.adapter_contract import AdapterContract

CST = timezone(timedelta(hours=8))
FIXTURES = Path("tests/fixtures/tushare")


def _raw(name: str, endpoint: str) -> RawResponse:
    return RawResponse(
        provider="tushare", endpoint=endpoint, params={},
        payload=json.loads((FIXTURES / name).read_text(encoding="utf-8")),
        http_status=200, fetched_at=datetime.now(timezone.utc),
    )


class TestTushareContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return TushareAdapter.for_replay(FIXTURES)


def test_known_at_uses_ann_date_not_end_date() -> None:
    """用 end_date 会让 9 月 30 日就「知道」三季报——典型前视偏差。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    records = list(adapter.parse(_raw("income_2024q3.json", "/income")))
    cam = next(r for r in records if r.entity_ref == "688256.SH")
    assert cam.known_at.astimezone(CST).date() == date(2024, 10, 28)
    assert cam.period_end == date(2024, 9, 30)
    assert cam.known_at.astimezone(CST).date() > cam.period_end


def test_known_at_is_end_of_announcement_day() -> None:
    """只给日期不给时间时取当日 23:59:59——宁可晚知道，不可早知道。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    rec = next(iter(adapter.parse(_raw("income_2024q3.json", "/income"))))
    local = rec.known_at.astimezone(CST)
    assert (local.hour, local.minute, local.second) == (23, 59, 59)


def test_parse_maps_all_metric_fields() -> None:
    adapter = TushareAdapter.for_replay(FIXTURES)
    fields = {r.metric_field for r in adapter.parse(_raw("income_2024q3.json", "/income"))}
    assert fields == {"revenue", "rd_exp"}


def test_price_known_at_is_market_close_not_fetch_time() -> None:
    """行情的 known_at 取 A 股当地收盘 15:30，不取拉取时间。"""
    adapter = TushareAdapter.for_replay(FIXTURES)
    rows = list(adapter.parse_prices(_raw("daily_20241028.json", "/daily")))
    assert len(rows) == 1
    local = rows[0].known_at.astimezone(CST)
    assert local.date() == date(2024, 10, 28)
    assert (local.hour, local.minute) == (15, 30)


def test_error_response_raises() -> None:
    adapter = TushareAdapter.for_replay(FIXTURES)
    bad = RawResponse(
        provider="tushare", endpoint="/income", params={},
        payload={"code": 40203, "msg": "积分不足", "data": None},
        http_status=200, fetched_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="40203"):
        list(adapter.parse(bad))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/adapters/test_tushare.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters.tushare'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/adapters/tushare.py`：

```python
"""Tushare Pro 适配器。

最关键的一行是 known_at 的取法：财务数据取 ann_date（公告日）当日 23:59:59，
不取 end_date（期末日）。取 end_date 会让 9 月 30 日就「知道」三季报——
这是最常见也最致命的前视偏差。见 docs/03-point-in-time.md §1.3。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse, require_aware
from ragdemo.adapters.http import HttpClient

CST = timezone(timedelta(hours=8))
A_SHARE_CLOSE_LOCAL = time(15, 30)
FACT_FIELDS = ("revenue", "rd_exp", "n_income", "total_assets", "total_hldr_eqy_exc_min_int")


@dataclass(frozen=True)
class PriceRow:
    entity_ref: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    pre_close: float | None
    volume: float | None
    amount: float | None
    adj_factor: float
    is_suspended: bool
    known_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(str(value), "%Y%m%d").date()


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Tushare 返回 fields + items 的列式结构，转成行式字典。"""
    if payload.get("code") != 0:
        raise ValueError(f"Tushare 返回错误 code={payload.get('code')}: {payload.get('msg')}")
    data = payload.get("data") or {}
    fields = data.get("fields", [])
    return [dict(zip(fields, item, strict=True)) for item in data.get("items", [])]


class TushareAdapter:
    """财务与行情。两类数据的 known_at 规则不同，因此分两个 parse 方法。"""

    provider = "tushare"

    def __init__(self, client: HttpClient | None = None, *, replay_dir: Path | None = None) -> None:
        if (client is None) == (replay_dir is None):
            raise ValueError("client 与 replay_dir 必须且只能提供一个")
        self._client = client
        self._replay_dir = replay_dir

    @classmethod
    def for_replay(cls, fixtures_dir: Path) -> TushareAdapter:
        """用录制的响应回放。契约测试与离线开发用。"""
        return cls(replay_dir=fixtures_dir)

    def health(self) -> bool:
        return True if self._replay_dir is not None else self._client is not None

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:
        if self._replay_dir is not None:
            for path in sorted(self._replay_dir.glob("*.json")):
                yield RawResponse(
                    provider=self.provider,
                    endpoint=f"/{path.stem}",
                    params={"partition": ctx.partition_date.isoformat()},
                    payload=json.loads(path.read_text(encoding="utf-8")),
                    http_status=200,
                    fetched_at=datetime.now(timezone.utc),
                )
            return
        assert self._client is not None
        yield self._client.get_json(
            "/income", {"period": ctx.partition_date.strftime("%Y%m%d"), **params}
        )

    def known_at(self, record: Any) -> datetime:
        """财务：公告日当日 23:59:59 CST。行情：交易日 15:30 CST。"""
        if "ann_date" in record:
            ann = _parse_yyyymmdd(record["ann_date"])
            return datetime.combine(ann, time(23, 59, 59), tzinfo=CST)
        trade = _parse_yyyymmdd(record["trade_date"])
        return datetime.combine(trade, A_SHARE_CLOSE_LOCAL, tzinfo=CST)

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        for row in _rows(raw.payload):
            period_end = _parse_yyyymmdd(row["end_date"])
            known_at = self.known_at(row)
            for field_name in FACT_FIELDS:
                if row.get(field_name) is None:
                    continue
                yield FactRecord(
                    entity_ref=str(row["ts_code"]),
                    metric_field=field_name,
                    period=_period_label(period_end),
                    period_end=period_end,
                    value=float(row[field_name]),
                    unit="CNY",
                    currency="CNY",
                    valid_from=date(period_end.year, 1, 1),
                    known_at=known_at,
                    source_ref=f"{row['ts_code']}:{row['end_date']}:{field_name}",
                )

    def parse_prices(self, raw: RawResponse) -> Iterator[PriceRow]:
        for row in _rows(raw.payload):
            yield PriceRow(
                entity_ref=str(row["ts_code"]),
                trade_date=_parse_yyyymmdd(row["trade_date"]),
                open=_maybe_float(row.get("open")),
                high=_maybe_float(row.get("high")),
                low=_maybe_float(row.get("low")),
                close=float(row["close"]),
                pre_close=_maybe_float(row.get("pre_close")),
                volume=_maybe_float(row.get("vol")),
                amount=_maybe_float(row.get("amount")),
                adj_factor=float(row.get("adj_factor", 1.0)),
                is_suspended=bool(row.get("vol") in (0, None)),
                known_at=self.known_at(row),
            )


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _period_label(period_end: date) -> str:
    if (period_end.month, period_end.day) == (12, 31):
        return f"{period_end.year}FY"
    return f"{period_end.year}Q{(period_end.month - 1) // 3 + 1}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/adapters/test_tushare.py -v`
Expected: 10 passed（5 条契约 + 5 条专属）

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters/tushare.py tests/adapters/test_tushare.py tests/fixtures/tushare
git commit -m "feat(adapters): Tushare 适配器，known_at 取公告日而非期末日"
```

---

## Task 7: EDGAR 适配器

**Files:**
- Create: `src/ragdemo/adapters/edgar.py`, `tests/fixtures/edgar/submissions_0001045810.json`, `tests/adapters/test_edgar.py`

**Interfaces:**
- Consumes: Task 1 协议、Task 5 的 `HttpClient`
- Produces:
  - `EdgarAdapter(client: HttpClient | None = None, *, replay_dir: Path | None = None)`，`provider = "edgar"`
  - `FilingRef(cik, accession, form_type, filing_date, acceptance_datetime, primary_document, document_url)`
  - `parse_filings(raw) -> Iterator[FilingRef]`
  - `TRACKED_FORMS: frozenset[str] = frozenset({"10-K", "10-Q", "8-K"})`

- [ ] **Step 1: 写失败的测试**

`tests/fixtures/edgar/submissions_0001045810.json`：

```json
{"cik": "1045810", "name": "NVIDIA CORP", "filings": {"recent": {
  "accessionNumber": ["0001045810-24-000316", "0001045810-24-000299", "0001045810-24-000250"],
  "form": ["10-Q", "8-K", "DEF 14A"],
  "filingDate": ["2024-11-20", "2024-11-20", "2024-05-30"],
  "acceptanceDateTime": ["2024-11-20T16:31:24.000Z", "2024-11-20T16:05:11.000Z", "2024-05-30T17:02:00.000Z"],
  "primaryDocument": ["nvda-20241027.htm", "nvda-8k.htm", "nvda-def14a.htm"]
}}}
```

`tests/adapters/test_edgar.py`：

```python
"""EDGAR 适配器：known_at 用 acceptanceDateTime（精确到秒），不用 filingDate。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ragdemo.adapters.base import Adapter, RawResponse
from ragdemo.adapters.edgar import TRACKED_FORMS, EdgarAdapter
from tests.contracts.adapter_contract import AdapterContract

FIXTURES = Path("tests/fixtures/edgar")


def _raw() -> RawResponse:
    return RawResponse(
        provider="edgar", endpoint="/submissions/CIK0001045810.json", params={},
        payload=json.loads(
            (FIXTURES / "submissions_0001045810.json").read_text(encoding="utf-8")
        ),
        http_status=200, fetched_at=datetime.now(UTC),
    )


class TestEdgarContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return EdgarAdapter.for_replay(FIXTURES)


def test_only_tracked_forms_are_returned() -> None:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    assert {f.form_type for f in filings} == {"10-Q", "8-K"}
    assert "DEF 14A" not in TRACKED_FORMS


def test_known_at_is_acceptance_datetime_to_the_second() -> None:
    """EDGAR 给出精确到秒的受理时间，直接用，不要退化成日期。"""
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    tenq = next(f for f in filings if f.form_type == "10-Q")
    assert tenq.acceptance_datetime == datetime(2024, 11, 20, 16, 31, 24, tzinfo=UTC)


def test_document_url_is_constructed_correctly() -> None:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    tenq = next(f for f in filings if f.form_type == "10-Q")
    assert tenq.document_url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581024000316/nvda-20241027.htm"
    )


def test_two_filings_same_day_are_ordered_by_acceptance_time() -> None:
    """同日两份申报，靠 acceptanceDateTime 才能分出先后。"""
    filings = [f for f in EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw())]
    same_day = sorted(
        (f for f in filings if f.filing_date.isoformat() == "2024-11-20"),
        key=lambda f: f.acceptance_datetime,
    )
    assert [f.form_type for f in same_day] == ["8-K", "10-Q"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/adapters/test_edgar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters.edgar'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/adapters/edgar.py`：

```python
"""SEC EDGAR 适配器。

EDGAR 是 P1 唯一免费且真实的语料来源。在公告供应商选定前，它承担
「用真实数据检验管线」的职责（adr/0005 的风险缓解），因此评测集中
要求至少 30 条基于 EDGAR 真实文档。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ragdemo.adapters.base import FetchContext, RawResponse, require_aware
from ragdemo.adapters.http import HttpClient

TRACKED_FORMS = frozenset({"10-K", "10-Q", "8-K"})
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


@dataclass(frozen=True)
class FilingRef:
    cik: str
    accession: str
    form_type: str
    filing_date: date
    acceptance_datetime: datetime
    primary_document: str

    def __post_init__(self) -> None:
        require_aware(self.acceptance_datetime, "acceptance_datetime")

    @property
    def document_url(self) -> str:
        return (
            f"{ARCHIVES}/{int(self.cik)}/"
            f"{self.accession.replace('-', '')}/{self.primary_document}"
        )


class EdgarAdapter:
    provider = "edgar"

    def __init__(self, client: HttpClient | None = None, *, replay_dir: Path | None = None) -> None:
        if (client is None) == (replay_dir is None):
            raise ValueError("client 与 replay_dir 必须且只能提供一个")
        self._client = client
        self._replay_dir = replay_dir

    @classmethod
    def for_replay(cls, fixtures_dir: Path) -> EdgarAdapter:
        return cls(replay_dir=fixtures_dir)

    def health(self) -> bool:
        return True if self._replay_dir is not None else self._client is not None

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:
        if self._replay_dir is not None:
            for path in sorted(self._replay_dir.glob("*.json")):
                yield RawResponse(
                    provider=self.provider,
                    endpoint=f"/submissions/{path.stem}.json",
                    params={"partition": ctx.partition_date.isoformat()},
                    payload=json.loads(path.read_text(encoding="utf-8")),
                    http_status=200,
                    fetched_at=datetime.now(tz=datetime.now().astimezone().tzinfo),
                )
            return
        assert self._client is not None
        cik = str(params["cik"]).zfill(10)
        yield self._client.get_json(f"/submissions/CIK{cik}.json", {})

    def known_at(self, record: Any) -> datetime:
        """acceptanceDateTime 是 EDGAR 公开受理该申报的精确时刻。"""
        return _parse_acceptance(record["acceptanceDateTime"])

    def parse_filings(self, raw: RawResponse) -> Iterator[FilingRef]:
        payload = raw.payload
        cik = str(payload["cik"])
        recent = payload["filings"]["recent"]
        count = len(recent["accessionNumber"])
        for i in range(count):
            form_type = str(recent["form"][i])
            if form_type not in TRACKED_FORMS:
                continue
            yield FilingRef(
                cik=cik,
                accession=str(recent["accessionNumber"][i]),
                form_type=form_type,
                filing_date=date.fromisoformat(str(recent["filingDate"][i])),
                acceptance_datetime=_parse_acceptance(recent["acceptanceDateTime"][i]),
                primary_document=str(recent["primaryDocument"][i]),
            )


def _parse_acceptance(value: str) -> datetime:
    """EDGAR 用 '2024-11-20T16:31:24.000Z'；Python 3.11 的 fromisoformat 不吃 Z。"""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/adapters/test_edgar.py -v`
Expected: 9 passed（5 条契约 + 4 条专属）

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters/edgar.py tests/adapters/test_edgar.py tests/fixtures/edgar
git commit -m "feat(adapters): EDGAR 适配器，known_at 取 acceptanceDateTime"
```

---

## Task 8: 公告供应商抽象与 Mock

见 [adr/0005](../../adr/0005-announcement-provider-abstraction.md)。归一化模型由**我们的检索需求**定义，
不由任何供应商定义。

**Files:**
- Create: `src/ragdemo/adapters/announcements.py`, `src/ragdemo/adapters/mock/announcements.py`, `tests/contracts/announcement_contract.py`, `tests/contracts/test_mock_announcements.py`, `docs/vendor-evaluation.md`

**Interfaces:**
- Consumes: Task 1 协议
- Produces:
  - `NormalizedBlock(ordinal, block_type, section_path, content, page, bbox, level)`
  - `NormalizedDocument(provider_doc_id, entity_ref, doc_type, title, period, publish_at, language, source_url, raw_bytes_ref, content_hash, is_correction, supersedes_provider_doc_id, page_count, blocks)`
  - `AnnouncementProvider` Protocol：`list_documents(ctx, since, until, entity_refs=None)`、`fetch_document(ctx, provider_doc_id)`、`normalize(raw) -> NormalizedDocument`、`disclosure_lag: timedelta`
  - `AnnouncementContract`（pytest 基类，6 条契约）
  - `MockAnnouncementProvider(documents=None, *, disclosure_lag=timedelta(0))`

- [ ] **Step 1: 写失败的测试**

`tests/contracts/announcement_contract.py`：

```python
"""任何 AnnouncementProvider 实现都必须通过的 6 条契约。

选定供应商后，新写的 adapter 必须原样通过这些测试——这是「定接口不定供应商」
能兑现的前提（adr/0005）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ragdemo.adapters.announcements import AnnouncementProvider
from ragdemo.adapters.base import FetchContext

UTC = timezone.utc


class AnnouncementContract:
    def make_provider(self) -> AnnouncementProvider:
        raise NotImplementedError

    def make_context(self) -> FetchContext:
        return FetchContext(ingest_run_id="contract", partition_date=date(2024, 10, 28))

    def _documents(self) -> list[object]:
        provider = self.make_provider()
        ctx = self.make_context()
        return [
            provider.normalize(raw)
            for raw in provider.list_documents(
                ctx, since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
            )
        ]

    def test_publish_at_is_timezone_aware(self) -> None:
        for doc in self._documents():
            assert doc.publish_at.tzinfo is not None  # type: ignore[attr-defined]

    def test_block_ordinals_start_at_zero_and_are_contiguous(self) -> None:
        for doc in self._documents():
            ordinals = [b.ordinal for b in doc.blocks]  # type: ignore[attr-defined]
            assert ordinals == list(range(len(ordinals)))

    def test_every_block_has_nonempty_content(self) -> None:
        for doc in self._documents():
            for block in doc.blocks:  # type: ignore[attr-defined]
                assert block.content.strip()

    def test_block_types_are_from_the_allowed_set(self) -> None:
        allowed = {"paragraph", "table", "figure", "title"}
        for doc in self._documents():
            for block in doc.blocks:  # type: ignore[attr-defined]
                assert block.block_type in allowed

    def test_content_hash_is_stable_across_calls(self) -> None:
        first = {d.provider_doc_id: d.content_hash for d in self._documents()}  # type: ignore[attr-defined]
        second = {d.provider_doc_id: d.content_hash for d in self._documents()}  # type: ignore[attr-defined]
        assert first == second

    def test_disclosure_lag_is_a_non_negative_timedelta(self) -> None:
        lag = self.make_provider().disclosure_lag
        assert isinstance(lag, timedelta)
        assert lag >= timedelta(0)
```

`tests/contracts/test_mock_announcements.py`：

```python
"""MockAnnouncementProvider 通过全部公告契约，并正确应用披露延迟。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ragdemo.adapters.announcements import AnnouncementProvider, known_at_for
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from tests.contracts.announcement_contract import AnnouncementContract


class TestMockAnnouncementContract(AnnouncementContract):
    def make_provider(self) -> AnnouncementProvider:
        return MockAnnouncementProvider()


def test_known_at_adds_disclosure_lag() -> None:
    """T+1 批量供应商：我们能获知的时刻比公告发布时刻晚一天。"""
    publish_at = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    assert known_at_for(publish_at, timedelta(days=1)) == publish_at + timedelta(days=1)


def test_zero_lag_means_known_at_equals_publish_at() -> None:
    publish_at = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    assert known_at_for(publish_at, timedelta(0)) == publish_at


def test_mock_exposes_a_table_block_with_section_path() -> None:
    """Mock 数据必须包含表格块，否则下游切块器的表格分支永远测不到。"""
    provider = MockAnnouncementProvider()
    ctx = provider_ctx = None  # 占位以保持行短
    del ctx, provider_ctx
    docs = [
        provider.normalize(raw)
        for raw in provider.list_documents(
            _ctx(), since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]
    tables = [b for d in docs for b in d.blocks if b.block_type == "table"]
    assert tables
    assert all(t.section_path for t in tables)


def test_correction_document_points_at_the_original() -> None:
    provider = MockAnnouncementProvider()
    docs = [
        provider.normalize(raw)
        for raw in provider.list_documents(
            _ctx(), since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]
    corrections = [d for d in docs if d.is_correction]
    assert corrections
    assert all(c.supersedes_provider_doc_id for c in corrections)


def _ctx():  # type: ignore[no-untyped-def]
    from datetime import date

    from ragdemo.adapters.base import FetchContext

    return FetchContext(ingest_run_id="t", partition_date=date(2024, 10, 28))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/contracts/test_mock_announcements.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.adapters.announcements'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/adapters/announcements.py`：

```python
"""公告供应商的归一化模型与协议。

这份模型由我们的检索需求定义，不由任何供应商定义（adr/0005）。
选定供应商后只需写一个实现 AnnouncementProvider 的 adapter，
并原样通过 tests/contracts/announcement_contract.py 的 6 条契约。
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from ragdemo.adapters.base import FetchContext, RawResponse, require_aware

BLOCK_TYPES = frozenset({"paragraph", "table", "figure", "title"})


@dataclass(frozen=True)
class NormalizedBlock:
    ordinal: int
    block_type: str
    section_path: str
    content: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    level: int | None = None

    def __post_init__(self) -> None:
        if self.block_type not in BLOCK_TYPES:
            raise ValueError(f"未知 block_type: {self.block_type!r}")
        if not self.content.strip():
            raise ValueError(f"第 {self.ordinal} 块内容为空")


@dataclass(frozen=True)
class NormalizedDocument:
    provider_doc_id: str
    entity_ref: str | None
    doc_type: str
    title: str
    period: str | None
    publish_at: datetime
    language: str
    source_url: str | None
    raw_bytes_ref: str | None
    content_hash: str
    is_correction: bool
    supersedes_provider_doc_id: str | None
    page_count: int | None
    blocks: Sequence[NormalizedBlock]

    def __post_init__(self) -> None:
        require_aware(self.publish_at, "publish_at")
        ordinals = [b.ordinal for b in self.blocks]
        if ordinals != list(range(len(ordinals))):
            raise ValueError(f"{self.provider_doc_id} 的 block ordinal 不连续: {ordinals}")


def known_at_for(publish_at: datetime, disclosure_lag: timedelta) -> datetime:
    """把发布时刻换算成我们能获知的时刻。见 docs/03-point-in-time.md §1.4。"""
    require_aware(publish_at, "publish_at")
    if disclosure_lag < timedelta(0):
        raise ValueError(f"disclosure_lag 不能为负: {disclosure_lag}")
    return publish_at + disclosure_lag


@runtime_checkable
class AnnouncementProvider(Protocol):
    provider: str

    @property
    def disclosure_lag(self) -> timedelta:
        """相对真实发布时间的获知延迟。实时推送为 0；T+1 批量为 1 天。"""

    def health(self) -> bool: ...

    def list_documents(
        self,
        ctx: FetchContext,
        since: datetime,
        until: datetime,
        entity_refs: Sequence[str] | None = None,
    ) -> Iterator[RawResponse]: ...

    def fetch_document(self, ctx: FetchContext, provider_doc_id: str) -> RawResponse: ...

    def normalize(self, raw: RawResponse) -> NormalizedDocument: ...

    def known_at(self, record: Any) -> datetime: ...
```

`src/ragdemo/adapters/mock/announcements.py`：

```python
"""Mock 公告供应商。

数据取自真实公告的脱敏节选——Mock 太理想会让 P1 的评测数字虚高，
选型时才发现落差（adr/0005 的风险一节）。必须包含：正文段落、表格、更正公告。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ragdemo.adapters.announcements import (
    NormalizedBlock,
    NormalizedDocument,
    known_at_for,
)
from ragdemo.adapters.base import FetchContext, RawResponse

CST = timezone(timedelta(hours=8))

_DOCS: tuple[dict[str, Any], ...] = (
    {
        "id": "SSE-688256-2024Q3",
        "entity_ref": "688256.SH",
        "doc_type": "quarterly",
        "title": "寒武纪 2024 年第三季度报告",
        "period": "2024Q3",
        "publish_at": "2024-10-28T18:32:00+08:00",
        "is_correction": False,
        "supersedes": None,
        "page_count": 24,
        "blocks": [
            ("title", "第三节 主营业务", 1, 11, 1),
            ("paragraph",
             "报告期内公司智能计算集群系统业务实现营业收入 12,340 万元，同比增长 58.2%，"
             "主要系云端训练芯片出货量提升所致。", 1, 12, None),
            ("table",
             "| 业务分部 | 收入(万元) | 同比 |\n| 智能计算 | 12,340 | +58.2% |\n"
             "| 其他 | 1,020 | -3.1% |", 1, 13, None),
            ("paragraph",
             "研发费用 1,890 万元，同比增长 22.4%，主要用于下一代训练芯片流片。", 1, 14, None),
        ],
    },
    {
        "id": "SSE-688256-2024Q3-CORR",
        "entity_ref": "688256.SH",
        "doc_type": "announcement",
        "title": "关于 2024 年第三季度报告更正的公告",
        "period": "2024Q3",
        "publish_at": "2025-01-15T19:00:00+08:00",
        "is_correction": True,
        "supersedes": "SSE-688256-2024Q3",
        "page_count": 2,
        "blocks": [
            ("paragraph",
             "经复核，公司 2024 年第三季度智能计算集群系统业务营业收入应为 12,500 万元，"
             "原披露 12,340 万元有误，特此更正。", 1, 1, None),
        ],
    },
)


class MockAnnouncementProvider:
    provider = "mock-announcements"

    def __init__(
        self,
        documents: Sequence[dict[str, Any]] | None = None,
        *,
        disclosure_lag: timedelta = timedelta(0),
    ) -> None:
        self._docs = tuple(documents) if documents is not None else _DOCS
        self._lag = disclosure_lag

    @property
    def disclosure_lag(self) -> timedelta:
        return self._lag

    def health(self) -> bool:
        return True

    def list_documents(
        self,
        ctx: FetchContext,
        since: datetime,
        until: datetime,
        entity_refs: Sequence[str] | None = None,
    ) -> Iterator[RawResponse]:
        for doc in self._docs:
            published = datetime.fromisoformat(doc["publish_at"])
            if not (since <= published <= until):
                continue
            if entity_refs is not None and doc["entity_ref"] not in entity_refs:
                continue
            yield RawResponse(
                provider=self.provider,
                endpoint="/documents",
                params={"since": since.isoformat(), "until": until.isoformat()},
                payload=doc,
                http_status=200,
                fetched_at=datetime.now(CST),
            )

    def fetch_document(self, ctx: FetchContext, provider_doc_id: str) -> RawResponse:
        doc = next(d for d in self._docs if d["id"] == provider_doc_id)
        return RawResponse(
            provider=self.provider, endpoint=f"/documents/{provider_doc_id}",
            params={}, payload=doc, http_status=200, fetched_at=datetime.now(CST),
        )

    def known_at(self, record: Any) -> datetime:
        return known_at_for(datetime.fromisoformat(record["publish_at"]), self._lag)

    def normalize(self, raw: RawResponse) -> NormalizedDocument:
        doc = raw.payload
        blocks = [
            NormalizedBlock(
                ordinal=i,
                block_type=block_type,
                section_path=_section_path(doc["blocks"], i),
                content=content,
                page=page,
                level=level,
            )
            for i, (block_type, content, _, page, level) in enumerate(doc["blocks"])
        ]
        body = "\n".join(b.content for b in blocks)
        return NormalizedDocument(
            provider_doc_id=doc["id"],
            entity_ref=doc["entity_ref"],
            doc_type=doc["doc_type"],
            title=doc["title"],
            period=doc["period"],
            publish_at=datetime.fromisoformat(doc["publish_at"]),
            language="zh",
            source_url=None,
            raw_bytes_ref=None,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            is_correction=bool(doc["is_correction"]),
            supersedes_provider_doc_id=doc["supersedes"],
            page_count=doc["page_count"],
            blocks=blocks,
        )


def _section_path(raw_blocks: Sequence[tuple[Any, ...]], index: int) -> str:
    """向前找最近的 title 块作为章节路径。"""
    for i in range(index, -1, -1):
        if raw_blocks[i][0] == "title":
            return str(raw_blocks[i][1])
    return ""
```

`docs/vendor-evaluation.md`：

```markdown
# 公告供应商评估清单

工作流 W9.2（[`11-sdlc.md`](11-sdlc.md) §3）。逐项打分，作为采购谈判的技术附件。
六项来自 [`04-ingestion.md`](04-ingestion.md) §2.3。

| # | 能力 | 为什么要它 | 打分 |
|---|---|---|---|
| 1 | 段落与表格分离，表格给结构化行列而非扁平文本 | 表格块被切碎就无法理解，直接拉低抽取准确率 | |
| 2 | 提供页码；`bbox` 是否可用 | 页码是 P2 溯源的最低要求，`bbox` 是 P4 点击高亮的前提 | |
| 3 | 提供章节标题层级 | 父子块构造依赖它（`05-document-pipeline.md` §4） | |
| 4 | 更正公告有显式标记与对原公告的指向 | 没有它就无法自动走更正流程，只能人工发现 | |
| 5 | 历史回溯深度 ≥ 3 年 | 回测需要 | |
| 6 | 精确到分钟的发布时间戳 | 只给日期则 `known_at` 退化成当日 23:59:59，日内事件研究不可用 | |

**验收方式**：拿到试用账号后，为其写一个 `AnnouncementProvider` 实现，
跑 `tests/contracts/announcement_contract.py` 的 6 条契约。全绿才算技术可用。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/contracts -v`
Expected: 17 passed（Mock 事实 7 + 公告契约 6 + 公告专属 4）

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/adapters/announcements.py src/ragdemo/adapters/mock/announcements.py tests/contracts docs/vendor-evaluation.md
git commit -m "feat(adapters): 公告归一化模型、AnnouncementProvider 契约与 Mock"
```

---

## Task 9: 实体解析三层降级

**Files:**
- Create: `src/ragdemo/entities/__init__.py`, `src/ragdemo/entities/resolver.py`, `tests/entities/__init__.py`, `tests/entities/test_resolver.py`

**Interfaces:**
- Consumes: P0 的 `core.entity` / `core.entity_alias` / `core.entity_resolution_queue`
- Produces:
  - `Resolution(entity_id: str | None, confidence: str, layer: str, candidates: list[Candidate])`
  - `Candidate(entity_id: str, score: float, reason: str)`
  - `EntityResolver(conn)`
    - `resolve_code(code: str) -> Resolution`
    - `resolve_name(name: str, *, context_entities: Sequence[str] = (), doc_type: str | None = None) -> Resolution`
    - `enqueue_unresolved(resolution, *, raw_ref, context, source, ingest_run_id) -> int`

- [ ] **Step 1: 写失败的测试**

`tests/entities/__init__.py`：空文件。

`tests/entities/test_resolver.py`：

```python
"""实体解析三层降级：代码 → 别名 → 上下文。三层都不确定则进人工队列，不猜。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate
from ragdemo.entities.resolver import EntityResolver

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def resolver(temp_db: str) -> EntityResolver:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type,"
        " l1_layer, l2_segment, l3_node, primary_node, tushare_code) VALUES "
        "('CN.688256','寒武纪-U','寒武纪','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH'),"
        "('CN.000063','中兴通讯','中兴','listed','算力','通信设备',"
        " ARRAY['服务器'],'服务器','000063.SZ'),"
        "('CN.002371','北方华创','北方华创','listed','算力','半导体设备',"
        " ARRAY['前道设备'],'前道设备','002371.SZ')"
    )
    conn.execute(
        "INSERT INTO core.entity_alias (alias, entity_id, alias_type, source) VALUES "
        "('寒武纪','CN.688256','short','manual'),"
        "('Cambricon','CN.688256','en','manual'),"
        "('中兴','CN.000063','short','manual'),"
        "('中兴','CN.002371','nickname','manual')"   # 故意制造一对多
    )
    conn.commit()
    return EntityResolver(conn)


@pytest.mark.db
def test_layer1_code_match_is_high_confidence(resolver: EntityResolver) -> None:
    r = resolver.resolve_code("688256.SH")
    assert (r.entity_id, r.layer, r.confidence) == ("CN.688256", "code", "high")


@pytest.mark.db
def test_layer1_unknown_code_resolves_to_none(resolver: EntityResolver) -> None:
    assert resolver.resolve_code("999999.SH").entity_id is None


@pytest.mark.db
def test_layer2_unique_alias_resolves(resolver: EntityResolver) -> None:
    r = resolver.resolve_name("寒武纪")
    assert (r.entity_id, r.layer) == ("CN.688256", "alias")


@pytest.mark.db
def test_layer3_ambiguous_alias_uses_context(resolver: EntityResolver) -> None:
    """「中兴」一对多，靠同文档共现实体消歧。"""
    r = resolver.resolve_name("中兴", context_entities=["CN.002371"])
    assert (r.entity_id, r.layer) == ("CN.002371", "context")


@pytest.mark.db
def test_ambiguous_without_context_returns_candidates_not_a_guess(
    resolver: EntityResolver,
) -> None:
    """没有上下文就不猜——把候选交出去，由人来定。"""
    r = resolver.resolve_name("中兴")
    assert r.entity_id is None
    assert {c.entity_id for c in r.candidates} == {"CN.000063", "CN.002371"}


@pytest.mark.db
def test_fuzzy_match_is_never_auto_accepted(resolver: EntityResolver) -> None:
    """「中兴新材」不得被自动判成「中兴通讯」。这类污染会扩散到关系图和观点。"""
    r = resolver.resolve_name("中兴新材")
    assert r.entity_id is None
    assert r.confidence == "low"


@pytest.mark.db
def test_unresolved_goes_to_the_queue(resolver: EntityResolver) -> None:
    r = resolver.resolve_name("中兴")
    qid = resolver.enqueue_unresolved(
        r, raw_ref="中兴", context={"doc_id": 1}, source="mock", ingest_run_id="r1"
    )
    row = resolver.conn.execute(
        "SELECT raw_ref, candidates, resolved_at FROM core.entity_resolution_queue"
        " WHERE id = %s",
        (qid,),
    ).fetchone()
    assert row is not None
    assert row[0] == "中兴"
    assert len(row[1]) == 2
    assert row[2] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/entities -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.entities'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/entities/__init__.py`：

```python
"""实体解析与消歧。"""
```

`src/ragdemo/entities/resolver.py`：

```python
"""实体解析三层降级（docs/04-ingestion.md §4）。

1. 代码精确匹配 → high
2. 别名精确匹配（唯一）→ high
3. 上下文打分（共现实体）→ medium

三层都不确定就**不猜**，进 core.entity_resolution_queue 等人工处理。
模糊匹配（pg_trgm）只用于给人工提供候选，绝不自动采纳——把「中兴新材」
匹配成「中兴通讯」这类错误一旦入库，污染会扩散到关系图和观点。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

FUZZY_SIMILARITY_FLOOR = 0.4


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    score: float
    reason: str


@dataclass(frozen=True)
class Resolution:
    entity_id: str | None
    confidence: str
    layer: str
    candidates: list[Candidate] = field(default_factory=list)


class EntityResolver:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def resolve_code(self, code: str) -> Resolution:
        """第 1 层：供应商代码精确匹配。"""
        row = self.conn.execute(
            "SELECT entity_id FROM core.entity "
            " WHERE tushare_code = %s OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
            (code, code, code, code),
        ).fetchone()
        if row is None:
            return Resolution(None, "low", "code")
        return Resolution(str(row[0]), "high", "code")

    def resolve_name(
        self,
        name: str,
        *,
        context_entities: Sequence[str] = (),
        doc_type: str | None = None,
    ) -> Resolution:
        """第 2–3 层：别名精确匹配，命中多个时用上下文消歧。"""
        exact = [
            str(r[0])
            for r in self.conn.execute(
                "SELECT entity_id FROM core.entity_alias WHERE alias = %s", (name,)
            ).fetchall()
        ]

        if len(exact) == 1:
            return Resolution(exact[0], "high", "alias")

        if len(exact) > 1:
            candidates = [Candidate(e, 1.0, "别名精确匹配") for e in exact]
            in_context = [e for e in exact if e in context_entities]
            if len(in_context) == 1:
                return Resolution(in_context[0], "medium", "context", candidates)
            return Resolution(None, "low", "alias_ambiguous", candidates)

        return Resolution(None, "low", "fuzzy", self._fuzzy_candidates(name))

    def _fuzzy_candidates(self, name: str) -> list[Candidate]:
        """模糊候选只供人工参考，永不自动采纳。"""
        rows = self.conn.execute(
            "SELECT entity_id, similarity(alias, %s) AS s FROM core.entity_alias "
            " WHERE similarity(alias, %s) > %s ORDER BY s DESC LIMIT 5",
            (name, name, FUZZY_SIMILARITY_FLOOR),
        ).fetchall()
        return [Candidate(str(r[0]), float(r[1]), "模糊匹配（需人工确认）") for r in rows]

    def enqueue_unresolved(
        self,
        resolution: Resolution,
        *,
        raw_ref: str,
        context: Mapping[str, Any],
        source: str,
        ingest_run_id: str,
    ) -> int:
        """写入人工消歧队列。队列长度是数据质量的日常监控指标。"""
        if resolution.entity_id is not None:
            raise ValueError("已解析的实体不应进入人工队列")
        row = self.conn.execute(
            "INSERT INTO core.entity_resolution_queue "
            " (raw_ref, context, candidates, source, ingest_run_id) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (
                raw_ref,
                Jsonb(dict(context)),
                Jsonb(
                    [
                        {"entity_id": c.entity_id, "score": c.score, "reason": c.reason}
                        for c in resolution.candidates
                    ]
                ),
                source,
                ingest_run_id,
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/entities -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/entities tests/entities
git commit -m "feat(entities): 实体解析三层降级，模糊匹配不自动采纳"
```

---

## Task 10: Dagster 资产、分区与传感器

**Files:**
- Create: `src/ragdemo/ingest/assets.py`, `src/ragdemo/ingest/definitions.py`, `tests/ingest/test_assets.py`
- Modify: `pyproject.toml`（加 `dagster`、`dagster-webserver`）, `Makefile`

**Interfaces:**
- Consumes: Task 2/3/4/6/7 的适配器与中间件
- Produces:
  - `DAILY = DailyPartitionsDefinition(start_date="2022-01-01", timezone="Asia/Shanghai")`
  - 资产 `tushare_daily_fin` / `fact_normalized` / `fin_fact_loaded`
  - `new_document_sensor`
  - Makefile 目标 `dagster`

- [ ] **Step 1: 写失败的测试**

`tests/ingest/test_assets.py`：

```python
"""Dagster 资产：幂等、分区起点、回填时 known_at 落在历史区间。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import psycopg
import pytest
from dagster import build_asset_context

from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.db.migrate import migrate
from ragdemo.ingest.assets import DAILY, fact_normalized, fin_fact_loaded
from ragdemo.ingest.writer import PointInTimeWriter, WriteOutcome

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES "
        "('CN.688256','寒武纪-U','listed','算力','AI芯片',ARRAY['云端训练芯片'],"
        " '云端训练芯片','688256.SH'),"
        "('CN.002049','紫光国微','listed','算力','AI芯片',ARRAY['云端训练芯片'],"
        " '云端训练芯片','002049.SZ')"
    )
    for metric in ("revenue_total", "rd_expense"):
        c.execute(
            "INSERT INTO core.node_metric (metric_id, metric_name, metric_role,"
            " frequency, source_type, definition, unit) "
            "VALUES (%s,%s,'confirming','quarterly','filing','口径','CNY')",
            (metric, metric),
        )
        c.execute(
            "INSERT INTO core.metric_source_map (metric_id, provider, provider_field) "
            "VALUES (%s,'mock',%s)",
            (metric, metric),
        )
    c.commit()
    return c


def test_partition_starts_2022_for_three_years_of_backtest() -> None:
    assert DAILY.start.date() == date(2022, 1, 1)


@pytest.mark.db
def test_fact_normalized_produces_records(conn: psycopg.Connection) -> None:
    ctx = build_asset_context(partition_key="2024-10-28")
    records = fact_normalized(ctx, MockFactAdapter())
    assert records
    assert all(r.known_at.tzinfo is not None for r in records)


@pytest.mark.db
def test_asset_is_idempotent_across_reruns(conn: psycopg.Connection) -> None:
    """同一分区重跑两次，第二次全部 SKIPPED，表里仍只有一份数据。"""
    ctx = build_asset_context(partition_key="2024-10-28")
    records = fact_normalized(ctx, MockFactAdapter())

    writer = PointInTimeWriter(conn, ingest_run_id="run-1", source="mock")
    first = fin_fact_loaded(ctx, records, writer)
    (after_first,) = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]

    writer2 = PointInTimeWriter(conn, ingest_run_id="run-2", source="mock")
    second = fin_fact_loaded(ctx, records, writer2)
    (after_second,) = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]

    assert first[WriteOutcome.INSERTED] == 3
    assert second.get(WriteOutcome.INSERTED, 0) == 0
    assert second[WriteOutcome.SKIPPED_IDENTICAL] == 3
    assert after_first == after_second == 3


@pytest.mark.db
def test_backfilling_an_old_partition_keeps_known_at_historical(
    conn: psycopg.Connection,
) -> None:
    """回填 2022 年的分区，known_at 必须是 2022 年，不是今天。"""
    ctx = build_asset_context(partition_key="2022-03-15")
    records = fact_normalized(ctx, MockFactAdapter())
    writer = PointInTimeWriter(conn, ingest_run_id="backfill", source="mock")
    fin_fact_loaded(ctx, records, writer)

    (known_at,) = conn.execute("SELECT min(known_at) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert known_at.year == 2022
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/ingest/test_assets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.ingest.assets'`

- [ ] **Step 3: 写最小实现**

`pyproject.toml` 改为（`dependencies` 新增 `dagster`，`dev` 新增 `dagster-webserver`）：

```toml
dependencies = [
    "psycopg[binary]>=3.2",
    "click>=8.1",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "dagster>=1.8",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
    "mypy>=1.11",
    "pre-commit>=3.8",
    "detect-secrets>=1.5",
    "dagster-webserver>=1.8",
]
```

`src/ragdemo/ingest/assets.py`：

```python
"""Dagster 资产与日分区。

两条不可妥协的规则：
1. 资产必须幂等——同一分区重跑结果一致。实现靠时点写入中间件的 SKIPPED_IDENTICAL，
   不用 ON CONFLICT DO UPDATE（那会破坏时点语义）。
2. 回填历史分区时 known_at 必须是历史时刻。适配器的 known_at() 从记录本身计算，
   因此回填天然正确——这是把该逻辑放在适配器而非中间件的理由。
"""
from __future__ import annotations

from datetime import date

from dagster import AssetExecutionContext, DailyPartitionsDefinition, asset

from ragdemo.adapters.base import Adapter, FactRecord, FetchContext
from ragdemo.ingest.writer import PointInTimeWriter, WriteOutcome

DAILY = DailyPartitionsDefinition(start_date="2022-01-01", timezone="Asia/Shanghai")


def _context_for(ctx: AssetExecutionContext) -> FetchContext:
    return FetchContext(
        ingest_run_id=ctx.run_id,
        partition_date=date.fromisoformat(ctx.partition_key),
    )


@asset(partitions_def=DAILY, group_name="ingest")
def fact_normalized(ctx: AssetExecutionContext, adapter: Adapter) -> list[FactRecord]:
    """拉取并归一化。不写库——入库是下一个资产的事。"""
    fetch_ctx = _context_for(ctx)
    records: list[FactRecord] = []
    for raw in adapter.fetch(fetch_ctx):
        records.extend(adapter.parse(raw))  # type: ignore[attr-defined]
    ctx.log.info(
        "normalized", extra={"run_id": fetch_ctx.ingest_run_id, "count": len(records)}
    )
    return records


@asset(partitions_def=DAILY, group_name="ingest")
def fin_fact_loaded(
    ctx: AssetExecutionContext,
    fact_normalized: list[FactRecord],
    writer: PointInTimeWriter,
) -> dict[WriteOutcome, int]:
    """经时点写入中间件入库。重跑同一分区时全部走 SKIPPED_IDENTICAL。"""
    counts = writer.write_facts(fact_normalized)
    ctx.log.info(
        "loaded",
        extra={
            "run_id": ctx.run_id,
            "as_of": None,
            "counts": {k.value: v for k, v in counts.items()},
        },
    )
    return counts
```

`src/ragdemo/ingest/definitions.py`：

```python
"""Dagster Definitions 与传感器。

衔接点是 core.event 表：Dagster 入库新文档后写 event 候选行，
LangGraph 侧的监听器按 event_id 取任务。**Dagster 资产不直接调用 Agent**——
避免数据管线被模型调用的延迟与失败拖垮（docs/01-architecture.md §3）。
"""
from __future__ import annotations

import os

import psycopg
from dagster import (
    DefaultSensorStatus,
    Definitions,
    RunRequest,
    SensorEvaluationContext,
    SkipReason,
    sensor,
)

from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.ingest.assets import fact_normalized, fin_fact_loaded
from ragdemo.ingest.writer import PointInTimeWriter

TRACKED_DOC_TYPES = ("quarterly", "annual_report", "announcement", "10-K", "10-Q", "8-K")


def _conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["RAGDEMO_DSN"])


@sensor(minimum_interval_seconds=300, default_status=DefaultSensorStatus.STOPPED)
def new_document_sensor(ctx: SensorEvaluationContext) -> RunRequest | SkipReason:
    """新文档入库后写 event 候选行。判定条件见 docs/07-agents.md §7。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT doc_id FROM core.document "
            " WHERE doc_type = ANY(%s) AND superseded_at IS NULL"
            "   AND doc_id > COALESCE(%s, 0) ORDER BY doc_id LIMIT 50",
            (list(TRACKED_DOC_TYPES), ctx.cursor),
        ).fetchall()
    if not rows:
        return SkipReason("无新文档")
    ctx.update_cursor(str(rows[-1][0]))
    return RunRequest(run_key=f"docs-{rows[-1][0]}", run_config={})


defs = Definitions(
    assets=[fact_normalized, fin_fact_loaded],
    sensors=[new_document_sensor],
    resources={
        "adapter": MockFactAdapter(),
        "writer": PointInTimeWriter(_conn(), ingest_run_id="dagster", source="mock"),
    },
)
```

Makefile 追加：

```makefile
.PHONY: dagster

dagster:
	dagster dev -m ragdemo.ingest.definitions
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pip install -e ".[dev]" && pytest tests/ingest/test_assets.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/ingest/assets.py src/ragdemo/ingest/definitions.py tests/ingest/test_assets.py pyproject.toml Makefile
git commit -m "feat(ingest): Dagster 日分区资产，幂等且回填保持历史 known_at"
```

---

## Self-Review

**Spec 覆盖检查**（对照 [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W1）：

| 工作流 | 出口条件 | 任务 | 覆盖 |
|---|---|---|---|
| W1.1 适配器抽象 + Mock + 契约 | 5 条契约对 Mock 通过 | Task 1, 2 | ✅ |
| W1.2 时点写入中间件 | 更正流程单测；漏打标记时唯一索引报错 | Task 3 | ✅ |
| W1.3 Tushare | 回放测试；`known_at` 取 `ann_date` | Task 6 | ✅ |
| W1.4 EDGAR | `known_at` = `acceptanceDateTime` | Task 7 | ✅ |
| W1.5 公告抽象 | 归一化模型可表达 6 项能力 | Task 8 | ✅ |
| W1.6 Dagster | 同分区重跑一致；回填 `known_at` 落历史区间 | Task 10 | ✅ |
| W1.7 实体解析 | 模糊匹配不自动采纳；队列可消费 | Task 9 | ✅ |
| 密钥不进 `provider_snapshot` | — | Task 4 | ✅ |
| 限流重试配额 | 重试耗尽让分区失败 | Task 5 | ✅ |

**类型一致性检查**：`FactRecord` 在 Task 1 定义，Task 2/3/6/10 使用，字段一致；
`RawResponse` 在 Task 1 定义，Task 4/5/6/7/8 使用；`FetchContext` 贯穿 Task 1/2/6/7/8/10；
`PointInTimeWriter.write_facts` 在 Task 3 定义，Task 10 使用，返回类型
`dict[WriteOutcome, int]` 一致；`AdapterContract.make_adapter` 在 Task 2 定义，
Task 6/7 子类化；`AnnouncementContract.make_provider` 在 Task 8 定义并自用。

**已知缺口（有意留给后续计划）**：

- 公告文档的**入库**（`NormalizedDocument` → `core.document` + `core.doc_block`）
  属于文档管线，在 [P1b](2026-09-21-p1b-document-pipeline.md) Task 1。
  本计划只交付归一化模型与 Provider 契约。
- `price_daily` 的入库方法未在 `PointInTimeWriter` 中实现——P1c 的观点评分才需要它，
  届时按 `write_fact` 的同一模式补 `write_price`。本计划的 `PriceRow`（Task 6）
  已经把数据结构定好。
- 出网代理（W7.6）不在本计划，属于 [P1c](2026-09-21-p1c-retrieval-and-acceptance.md)。
  在此之前 `HttpClient` 直连，密钥经环境变量注入。

---

## 完成之后

1. 在 [`docs/10-roadmap.md`](../../10-roadmap.md) P1 的对应 checklist 上打勾
   （`Adapter` 抽象、Tushare、公告抽象、EDGAR、时点中间件、实体解析、Dagster 七项）。
2. 把实测中与文档不符之处回写 [`docs/04-ingestion.md`](../../04-ingestion.md)。
3. 执行 [P1b 文档管线](2026-09-21-p1b-document-pipeline.md)。
