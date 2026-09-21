# 04 · 数据接入与适配器

## 1. 数据源清单

| 数据 | 来源 | 接入方式 | 阶段 | 频率 |
|---|---|---|---|---|
| A 股 / 港股财务、行情、股东、分部 | Tushare Pro | REST 批量 | P1 | 每日 |
| A 股 / 港股公告（结构化：段落、表格、页码） | 待选定供应商 | REST 批量 + MCP 按需补查 | P1 | 每日 |
| 美股 / 台股 / 韩股财务、电话会 | FMP / Financial Datasets | REST | P1 | 每日 |
| 美股申报原文 | SEC EDGAR | REST（免费） | P1 | 每日 |
| 台积电月营收、法说会 | 公司 IR 页 | 定向抓取 | P2 | 月度 / 季度 |
| 云厂商 Capex 指引 | 电话会文稿 | Agent 抽取 + 人工校验 | P2 | 季度 |
| 政策（BIS 出口管制、工信部、发改委） | 官网 | 定向抓取 + 结构化 | P3 | 每日轮询 |
| 新闻 | 财联社 / 新浪财经 / 海外 RSS | RSS | P3 | 15 分钟 |
| 模型与技术进展 | Hugging Face / arXiv / 公司博客 | RSS / API | P3 | 每日 |
| 用户上传材料 | 用户 | 上传 → MinerU → 私有空间 | P4 | 事件驱动 |

**接入原则**：批量同步走 REST / 数据文件；MCP 只做长尾按需查询；
所有供应商 MCP 一律包在自有工具外壳层内（参数校验、日志、缓存、限流、结果规范化）。

## 2. 适配器接口

### 2.1 基础抽象

所有适配器实现同一组协议，业务代码**只 import 这些协议，不 import 任何供应商 SDK**
（`CLAUDE.md` §1.5）。

```python
# src/adapters/base.py
from typing import Protocol, Iterator, Any
from datetime import datetime, date
from dataclasses import dataclass

@dataclass(frozen=True)
class FetchContext:
    """一次拉取的上下文。ingest_run_id 贯穿整条链路进入每一行数据。"""
    ingest_run_id: str
    partition_date: date
    dry_run: bool = False

@dataclass(frozen=True)
class RawResponse:
    """原始响应，入库 provider_snapshot 后才进入解析。"""
    provider: str
    endpoint: str
    params: dict[str, Any]
    payload: Any
    http_status: int
    fetched_at: datetime
    cost_cents: float | None = None

class Adapter(Protocol):
    provider: str

    def health(self) -> bool:
        """连通性与配额检查，Dagster 资产启动前调用。"""

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:
        """按分区拉取原始数据。必须是幂等的：同样的 ctx 产生同样的结果。"""

    def known_at(self, record: Any) -> datetime:
        """计算该条记录的 known_at。规则见 03-point-in-time.md §1.3。
        实现必须返回带时区的 datetime；返回 naive datetime 视为缺陷。"""
```

`known_at` 作为适配器的方法而非中间件的逻辑，是因为**每个数据源的获知时刻规则不同**
（公告用发布时间、财务用公告日、行情用收盘时刻），只有适配器知道原始字段的语义。

### 2.2 结构化事实适配器

```python
@dataclass(frozen=True)
class FactRecord:
    entity_ref: str          # 供应商侧代码，尚未解析为 entity_id
    metric_field: str        # 供应商侧字段名，尚未映射为 metric_id
    period: str
    period_end: date
    value: float
    unit: str
    currency: str | None
    valid_from: date
    known_at: datetime
    source_ref: str | None

class FactAdapter(Adapter, Protocol):
    def parse(self, raw: RawResponse) -> Iterator[FactRecord]: ...
```

实现：`TushareAdapter`、`FmpAdapter`、`EdgarFactAdapter`。
`entity_ref` → `entity_id` 与 `metric_field` → `metric_id` 的映射在中间件里做
（分别查 `entity.tushare_code` 等列与 `metric_source_map`），适配器不负责。

### 2.3 `AnnouncementProvider`（供应商未定）

见 [adr/0005](adr/0005-announcement-provider-abstraction.md)。P1 只实现抽象接口、
归一化模型、Mock 与契约测试，选定供应商后补一个具体实现即可。

```python
@dataclass(frozen=True)
class NormalizedBlock:
    """归一化后的文档块。所有供应商的返回都要映射到这个形状。"""
    ordinal: int                    # 文档内顺序，从 0 开始，连续
    block_type: str                 # paragraph | table | figure | title
    section_path: str               # '第三节 主营业务 > 3.2 分部收入'；未知填 ''
    content: str                    # 表格块用 Markdown 表格文本
    page: int | None
    bbox: tuple[float, float, float, float] | None
    level: int | None               # title 块的层级，用于重建章节树

@dataclass(frozen=True)
class NormalizedDocument:
    provider_doc_id: str
    entity_ref: str | None
    doc_type: str
    title: str
    period: str | None
    publish_at: datetime            # 必须带时区
    language: str
    source_url: str | None
    raw_bytes_ref: str | None       # 原始 PDF 的对象存储 key
    content_hash: str               # 原始字节 sha256
    is_correction: bool
    supersedes_provider_doc_id: str | None
    page_count: int | None
    blocks: list[NormalizedBlock]

class AnnouncementProvider(Adapter, Protocol):
    def list_documents(self, ctx: FetchContext,
                       since: datetime, until: datetime,
                       entity_refs: list[str] | None = None) -> Iterator[RawResponse]: ...

    def fetch_document(self, ctx: FetchContext, provider_doc_id: str) -> RawResponse: ...

    def normalize(self, raw: RawResponse) -> NormalizedDocument: ...

    @property
    def disclosure_lag(self) -> timedelta:
        """该供应商相对真实发布时间的获知延迟，用于计算 known_at。
        实时推送为 0；T+1 批量为 1 天。见 03-point-in-time.md §1.4。"""
```

**选型时必须验证的六项能力**（写进供应商评估清单，也是契约测试的内容）：

1. 段落与表格是否分离，表格是否给出结构化行列而非扁平文本；
2. 是否提供页码；`bbox` 是否可用（影响溯源点击定位，P4 需要）；
3. 是否提供章节标题层级（影响父子块构造，`05-document-pipeline.md` §4）；
4. 更正公告是否有显式标记与对原公告的指向；
5. 历史回溯深度（至少需要 3 年，用于回测）；
6. 发布延迟是多少、是否提供精确到分钟的发布时间戳。

第 6 项若不满足（只给日期），`known_at` 按 `03-point-in-time.md` §1.3 取当日 23:59:59，
这会让日内事件研究不可用——评估时需明确这是否可接受。

### 2.4 MCP 外壳层

供应商 MCP **一律不得被 Agent 直接调用**，必须包在外壳内：

```python
# src/adapters/mcp_shell.py
class McpShell:
    """包裹外部 MCP 服务：参数校验 → 限流 → 缓存 → 调用 → 规范化 → 日志。"""

    def __init__(self, server: str, allowed_tools: set[str],
                 rate_limit_qps: float, cache_ttl_s: int,
                 budget_cents_per_run: float): ...

    def call(self, ctx: FetchContext, tool: str, params: dict) -> RawResponse:
        """未在 allowed_tools 中的工具直接拒绝；
        超出预算抛 BudgetExceeded；结果写 provider_snapshot 与 tool_call_log。"""
```

`allowed_tools` 白名单是安全边界：外部 MCP 服务器声明的工具列表是**不可信输入**，
不能因为服务器自称有某个工具就允许调用。

## 3. Dagster 资产与分区

### 3.1 分区

```python
daily = DailyPartitionsDefinition(start_date="2022-01-01", timezone="Asia/Shanghai")
```

起点 2022-01-01 覆盖回测所需的至少三年历史。

### 3.2 资产依赖

```
tushare_daily_price ──┐
tushare_daily_fin ────┼──► fact_normalized ──► fin_fact_loaded
fmp_daily_fin ────────┘

announcement_list ──► announcement_documents ──► doc_normalized ──┐
edgar_filings ──────────────────────────────────────────────────┬─┴─► doc_blocks_loaded
rss_news ───────────────────────────────────────────────────────┘          │
                                                                            ▼
                                                                    block_embeddings
                                                                            │
                                                                            ▼
                                                                    event_candidates
```

**每个资产必须幂等**：重跑同一分区产生同样的结果。实现方式是所有写入走
「查是否已存在相同 `content_hash` / `(entity, metric, period, known_at)`」→ 存在则跳过。
不用 `ON CONFLICT DO UPDATE`，因为更新会破坏时点语义（§3 更正流程是唯一的修改路径）。

### 3.3 回填

回填历史分区时**必须使用真实的历史 `known_at`**，不能用回填当天的时间
（`03-point-in-time.md` §1.2）。适配器的 `known_at()` 方法从记录本身计算，
因此回填天然正确——这是把该逻辑放在适配器而非中间件的另一个理由。

回填命令与并发上限（避免打爆供应商配额）写在 `infra/backfill.md`。

### 3.4 传感器

- `new_document_sensor`：`doc_blocks_loaded` 完成后，为符合条件的新文档写 `core.event`
  候选行，供 Agent 侧消费。判定条件见 `07-agents.md` §7。
- `provider_health_sensor`：每小时调用各适配器 `health()`，失败则告警。

## 4. 实体解析与消歧

供应商返回的 `entity_ref` 到 `entity_id` 的解析分三层，**逐层降级**：

1. **代码精确匹配** — `entity.tushare_code` / `ifind_code` / `wind_code` / `edgar_cik`。
   命中即确定，置信度 `high`。
2. **别名精确匹配** — `entity_alias.alias` 完全相等。若匹配到多个实体，进入第 3 层。
3. **上下文消歧** — 用同一文档中共现的其他实体、`doc_type`、发布方代码做加权打分。

三层都无法确定的记录**不猜**，写入 `core.entity_resolution_queue` 等待人工处理：

```sql
CREATE TABLE core.entity_resolution_queue (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raw_ref       text NOT NULL,
  context       jsonb NOT NULL,     -- 文档、共现实体、原文片段
  candidates    jsonb NOT NULL,     -- [{entity_id, score, reason}]
  source        text NOT NULL,
  ingest_run_id text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  resolved_to   text REFERENCES core.entity,
  resolved_by   text,
  resolved_at   timestamptz
);

CREATE INDEX entity_resolution_pending ON core.entity_resolution_queue (created_at)
  WHERE resolved_at IS NULL;
```

人工处理时同时把新别名写回 `entity_alias`，队列会自然收敛。
**队列长度是数据质量的日常监控指标**：持续增长说明别名库覆盖不足。

模糊匹配（`pg_trgm`）只用于**给人工提供候选**，绝不自动采纳——
把「中兴通讯」匹配到「中兴新材」这类错误一旦入库，污染会扩散到关系图和观点。

## 5. 限流、重试、成本

### 5.1 配置

每个适配器在 `config/providers.yaml` 声明：

```yaml
tushare:
  base_url: https://api.tushare.pro
  rate_limit_qpm: 200          # 每分钟请求数
  daily_quota: 50000           # 每日积分/调用上限
  timeout_s: 30
  retry:
    max_attempts: 4
    backoff: exponential       # 1s, 2s, 4s, 8s
    retry_on: [429, 500, 502, 503, 504, timeout]
  cost_per_call_cents: 0
```

### 5.2 重试原则

- **只重试幂等的读操作**；
- `429` 优先读 `Retry-After` 响应头，没有才用退避；
- 重试耗尽后**让分区失败**，不要吞掉异常返回部分数据——部分数据入库比没数据更糟，
  因为下游会以为数据完整。

### 5.3 成本控制

月度预算 5000 元（`00-overview.md` §3）是硬约束。每次外部调用都写
`provider_snapshot.cost_cents` 与 `audit.tool_call_log.cost_cents`，
Dagster 中有日度成本资产，超出月度预算的 80% 时告警、100% 时停止非必要拉取。

## 6. 契约测试

每个适配器有两类测试（`tests/contracts/`）：

**契约测试** — 针对 Mock 实现，断言接口行为：

- `known_at()` 返回带时区的时间，且不晚于 `now()`；
- 回填历史分区时 `known_at` 落在历史区间内，不等于当前时间；
- `fetch()` 对同一 `FetchContext` 两次调用返回相同结果（幂等）；
- `normalize()` 产出的 `blocks` 的 `ordinal` 从 0 连续递增；
- `content_hash` 对相同字节稳定。

**录制回放测试** — 用 `provider_snapshot` 中录制的真实响应回放：

- 存放在 `tests/fixtures/<provider>/`，**必须脱敏**（去掉密钥、账号、个人信息）；
- 每个供应商至少 3 个样本：正常、边界（空结果 / 超长文档）、更正公告；
- 供应商返回格式变化时这组测试会红，这是我们发现上游变更的主要手段。

Mock 实现（`src/adapters/mock/`）是一等公民而非测试脚手架：
P1 阶段整条管线靠它跑通，公告供应商选定前的所有下游开发都依赖它。
