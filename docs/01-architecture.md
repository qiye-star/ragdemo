# 01 · 系统架构

## 1. 分层结构

项目简报 §3 用 L1–L10 编号描述了十层，但图中顺序与编号不一致（L5 文档管线排在 L4 权限隔离之前）。
本文档**按数据流顺序重排**，并保留原编号以便与简报对照。

| 本文档层次 | 简报原编号 | 职责 | 详见 |
|---|---|---|---|
| A. 数据接入 | — | 供应商 API / MCP、公告接口、EDGAR、RSS、用户上传 | `04-ingestion.md` |
| B. 实体解析 | L1 | `entity` / `entity_alias` / `entity_relation`，别名消歧 | `02-data-model.md` §2 |
| C. 口径归一 | L2 | `node_metric` 指标字典、AI 收入拆分规则、财年与币种转换 | `02-data-model.md` §3 |
| D. 时点管理 | L3 | 双时间轴、更正处理、`as_of` 安全视图 | `03-point-in-time.md` |
| E. 文档管线 | L5 | 结构化公告落库、TextIn xParse 解析、切块、元数据、向量化 | `05-document-pipeline.md` |
| F. 权限隔离 | L4 | C 端私有空间；B 端多租户 / SSO / 项目隔离 / 审计 | `09-compliance-security.md` §3 |
| G. 检索路由 | L6 | 意图路由、结构化查询翻译、元数据过滤、混合检索、重排、父子块 | `06-retrieval.md` |
| H. Agent 层 | — | 产业链映射 → 基本面 → 事件 → 宏观政策 → 多空辩论 → 观点入库 → 复盘 | `07-agents.md` |
| I. 引用验证 | L7 | 输出约束、后置验证器、置信度规则 | `07-agents.md` §5 |
| J. 输出模板 | L8 | 公司追踪简报、环节周报、事件解读、对标报告、Excel 导出 | `07-agents.md` §6 |
| K. 反馈评测 | L10 | 三套评测集、观点评分、修正记录 | `08-evaluation.md` |
| ⊥ 工作流编排 | L9 | Dagster（数据管线，按日分区，可回填）+ LangGraph（Agent 流程），横向贯穿 | 本文档 §3 |
| ⊥ 可信执行 | — | 网络代理 + 凭据代管 + 域名白名单；执行轨迹哈希链 | `09-compliance-security.md` §4 |

## 2. 数据流

### 2.1 入库流（Dagster，按日分区）

```
供应商 API / RSS / 上传
        │
        ├─► provider_snapshot            原始响应留存，永不修改
        │
        ▼
   Adapter.fetch()                      统一适配器接口，返回归一化对象
        │
        ▼
   实体解析（别名 → entity_id）          命中失败进入待人工消歧队列
        │
        ▼
   口径归一（metric_id、币种、财年）
        │
        ▼
   时点写入中间件                        计算 known_at；识别更正 → 给旧行打 superseded_at
        │
        ├─► fin_fact / price_daily      结构化事实
        │
        └─► document → doc_block        文档 → 切块 → 嵌入（1024 维）
                                              │
                                              ├─► HNSW 向量索引
                                              └─► pg_search BM25 索引
```

**关键不变量**：时点写入中间件是**唯一**能写事实表与文档表的路径。任何绕过它的直接 INSERT
都会破坏时点一致性，因此这些表对应用角色只授予 `SELECT`（见 `03-point-in-time.md` §4）。

### 2.2 查询流（同步，毫秒级）

```
问题 + as_of
   │
   ▼
意图路由（小模型）──► 结构化查询？ ──► query_fin_fact()  ─┐
   │                                                      │
   └─────────────► 文本查询 ──► RetrievalService          │
                                  │                       │
                       元数据过滤（entity / doc_type /     │
                       known_at <= as_of）                 │
                                  │                       │
                       ┌──────────┴──────────┐            │
                   BM25 top-50          向量 top-50        │
                       └──────────┬──────────┘            │
                            加权 RRF 融合                   │
                                  │                       │
                          bge-reranker → top-10           │
                                  │                       │
                          父子块展开（子块命中 → 返回父小节）│
                                  │                       │
                                  ▼                       ▼
                             证据集（带 block_id / page / known_at）
```

### 2.3 生成流（LangGraph，异步，分钟级）

```
触发事件（新公告 / 新闻 / 财报）
   │
   ▼
产业链映射 Agent ──► 受影响实体列表（代码按 ai_revenue_pct × share_estimate 加权）
   │
   ├─► 基本面 Agent ──┐
   ├─► 事件解读 Agent ─┼─► 多空辩论（看多 / 看空 / 裁判三角色）
   └─► 宏观政策 Agent ─┘          │
                                  ▼
                          引用验证器（后置）
                                  │
                         ┌────────┴────────┐
                     通过 │                 │ 不通过
                         ▼                 ▼
                  write_opinion       退回重生成 / 标注 `推断`
                （人工确认节点）       并写 correction_log
                         │
                         ▼
                     opinion 表 ──（1M / 3M 后）──► 复盘评估 → opinion_score
```

**增量触发**：生成流只由新数据进入触发，不做每日全量重算（`CLAUDE.md` §1.6）。
触发条件与去重规则见 `07-agents.md` §7。

## 3. 编排层职责划分

两套编排器职责严格分开，**不得互相调用**：

| | Dagster | LangGraph |
|---|---|---|
| 管什么 | 数据管线：拉取、解析、切块、嵌入、入库 | Agent 流程：路由、分析、辩论、验证 |
| 触发方式 | 定时（日分区）+ 手工回填 | 事件驱动 + 用户请求 |
| 幂等性 | 必须幂等，同一分区重跑结果一致 | 不要求幂等，但必须可从检查点恢复 |
| 失败处理 | 分区级重试，失败分区可单独回填 | 节点级重试 + 检查点回放 |
| 状态存储 | Dagster 自有元数据表（schema `dagster`） | LangGraph checkpointer（schema `orchestration`） |

两者的衔接点是 `event` 表：Dagster 入库新文档后写入 `event` 行，LangGraph 侧的监听器
按 `event_id` 取任务。**Dagster 资产不直接调用 Agent**，避免数据管线被模型调用的延迟与失败拖垮。

## 4. 部署拓扑

单机 docker-compose 起步，全部服务容器化。**不依赖任何云厂商专有服务**——这是
[adr/0002](adr/0002-self-hosted-paradedb-single-db.md) 的直接后果，也让 P6 私有化交付无需重构。

```
云主机（起步：8C / 32G / 500G SSD）
│
├── paradedb          ParadeDB 官方镜像（PostgreSQL + pg_search + pgvector）
│                     端口不对公网开放；数据卷独立挂载
├── dagster-webserver  Dagster UI（内网 / VPN 访问）
├── dagster-daemon     调度与传感器
├── api                FastAPI（P1 起：内网只读诊断接口，adr/0010；P4 起对外产品接口）
├── worker             LangGraph Agent 执行器
├── egress-proxy       出网代理：域名白名单 + 凭据注入 + 调用日志
└── minio              原始 PDF、用户上传件、解析产物（JSON + Markdown）对象存储
```

**出网代理是强制的**。所有对外部 API 的调用（供应商、模型、嵌入）都经 `egress-proxy`：

- 密钥只存在于代理层，业务容器的环境变量中**没有**任何供应商密钥；
- 域名白名单，不在名单内的请求直接拒绝；
- 全量调用日志落 `tool_call_log`，用于成本核算与审计。

详见 `09-compliance-security.md` §4。

**备份**：ParadeDB 是自建的，没有托管 RDS 的自动备份。必须自建：
每日 `pg_dump` 到对象存储（保留 30 天）+ WAL 归档实现 PITR + 每月一次恢复演练。
恢复 runbook 是 P1 的验收项之一（`10-roadmap.md`）。

## 5. 仓库结构

uv workspace，两个包：`ragdemo-core` 是底层，`ragdemo` 是业务层。

```
ragdemo/
  CLAUDE.md                 # 原则、决策、工程约定（对 Claude Code 的强约束）
  pyproject.toml            # workspace 虚拟根：成员声明 + ruff/mypy/pytest 统一配置
  Makefile  uv.lock  .python-version
  infra/                    # docker-compose、egress-proxy 配置、备份脚本
  db/
    migrations/             # 版本化迁移脚本
    seed/                   # 环节表 + 实体 / 关系 / 规则 / 指标的 CSV 种子数据
  packages/
    ragdemo-core/           # 底层：无业务逻辑，谁都可以依赖它
      src/ragdemo_core/
        db/                 # 迁移执行器、as_of 会话、schema 不变量与泄漏自检
    ragdemo/                # 业务层，依赖 ragdemo-core
      src/ragdemo/
        cli.py              # ragdemo db migrate / seed / check
        seed/               # 种子 CSV 导入与环节表校验
        adapters/           # tushare / announcements / edgar / rss / mcp_shell
        ingest/             # dagster 资产、分区、时点写入中间件
        parse/              # xparse 封装、切块、元数据
        retrieval/          # 过滤、混合、重排、父子块、同义词
        entities/           # 解析、别名、消歧
        metrics/            # 指标字典、口径转换、计算
        agents/
          prompts/<agent>/<version>.md
        tools/              # Agent 工具外壳层
        templates/          # 简报 / 周报 / 对标模板与渲染
        audit/              # tool_call_log、哈希链
        api/                # Web 后端：P1 内网只读诊断接口（adr/0010），P4 起对外产品接口
  web/                      # 前端：P1 内部诊断界面静态资源（adr/0010），P4 起面向用户产品前端
  evals/                    # 三套评测集与脚本
  docs/                     # 本文档集
  tests/
    db/                     # 迁移、时点安全层、不变量、检索 SQL
    seed/                   # 种子导入
    contracts/              # 适配器契约测试
```

**分成两个包不是为了好看，是为了让下面那条依赖规则物理生效。**
`ragdemo-core` 的 `pyproject.toml` 不依赖 `ragdemo`，所以底层 import 业务层
在包解析层面就走不通，不再依赖代码评审去发现。
`tests/test_toolchain.py` 另有一条断言直接扫源码守着这条线。

### 模块依赖方向

依赖只能自上而下，**不允许反向或横向跨层 import**：

```
api / cli
    ↓
agents  →  tools  →  retrieval / metrics / entities
                          ↓
                       ingest（时点写入中间件）
                          ↓
                       adapters
                          ↓
                       数据库
```

具体地：`adapters/` 不得 import 任何其他 `src/` 子包；`agents/` 只能通过 `tools/`
访问数据，不得直接 import `retrieval/` 或写 SQL。这条约束让工具外壳层
（参数校验、`as_of` 强制、日志、缓存、预算）成为不可绕过的关卡。
