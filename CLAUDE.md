# CLAUDE.md — AI 产业链时点研究引擎

本文件是对在本仓库工作的 Claude Code 的强约束。**违反其中任何一条的代码不得合并。**
完整技术规范见 `docs/`，阅读入口 `docs/README.md`。

开工前先确认三件事：这个任务属于哪条工作流（`docs/11-sdlc.md` §3）、
它的 DoR 是否满足（同 §2.3）、当前阶段的实施计划怎么说
（`docs/superpowers/plans/`）。

---

## 0. 产品边界（合规硬约束，不可协商）

- **禁止输出买卖建议、目标价、评级、仓位建议**。所有产出物定位为「研究参考」。
  违禁词表与拦截实现见 `docs/09-compliance-security.md`。
- **每个数字必须可溯源**。输出中出现的任意数值，要么绑定 `[block_id | page]` 或
  `[metric_id | source | known_at]`，要么显式标注为 `推断`。没有第三种状态。
- **数据境内存储**。不得将原始文档、用户上传材料、实体财务数据发送到境外服务。
- **用户上传材料私有隔离**。不得进入公共检索空间，不得用于其他用户的检索结果。

## 1. 六条核心设计原则

### 1.1 时点一致（最高优先级）
每张事实表都带 `valid_from` / `known_at` / `superseded_at`。所有对外查询接口
**强制** `as_of` 参数，没有默认值。标准谓词：

```sql
WHERE known_at <= :as_of
  AND (superseded_at IS NULL OR superseded_at > :as_of)
```

`known_at` 的定义是**该数据在现实中最早可被获知的时刻**（公告/财报的 `publish_at`，
或供应商约定的发布延迟后的时刻），**不是入库时间**。入库时间叫 `ingested_at`，
它只用于运维排查，**永远不得进入 `as_of` 过滤**。

Agent 与应用层**只允许查询 `asof` schema 下的安全视图**，不得直接查基表。
理由与实现见 `docs/03-point-in-time.md`。

### 1.2 计算与生成分离
同比、环比、倍数、加权、超额收益——**全部由 Python 代码计算**。模型只决定「算什么」，
不决定「算出来是多少」。任何让模型直接产出数值的 prompt 都是缺陷。

### 1.3 强制溯源
生成链路上每个事实都必须携带来源标识。验证器在输出前做后置检查：扫描数值 →
比对 evidence 映射 → 不匹配则拦截或降级标注。

### 1.4 评测驱动
改动 prompt、切块策略、检索参数、模型——**必须跑三套评测集**
（`eval_retrieval` / `eval_extraction` / 观点评分），把数字贴进 PR 描述再合并。
指标下降的改动不合并，除非 PR 里写清楚为什么可以接受。

### 1.5 供应商可替换
所有外部数据源、模型、嵌入、重排都经统一适配器接口。业务代码不得 import 供应商 SDK。
入库时同时保留原始响应（`provider_snapshot`）与结构化结果。

### 1.6 增量触发
只有新公告 / 新闻 / 财报进入才触发相关 Agent。**不做每日全量重算。**

---

## 2. 已锁定的技术决策

变更这些决策需要新增一篇 ADR 并说明推翻条件。详见 `docs/adr/`。

| 项 | 决策 | ADR |
|---|---|---|
| 全文检索 | ParadeDB `pg_search`（tantivy BM25） | 0001 |
| 数据库与部署 | 自建 ParadeDB 单库，docker-compose 全栈，不用托管 RDS | 0002 |
| Agent 编排 | LangGraph + 自有 `Orchestrator` 封装 | 0003 |
| 嵌入 / 重排 | 托管 API，向量维度固定 **1024** | 0004 |
| 公告数据源 | 先定 `AnnouncementProvider` 抽象接口，不锁定供应商 | 0005 |
| 前端 | 面向用户的产品前端在 P4；P1 起可做只读内部诊断界面（内网、强制 as_of） | 0006 / 0010 |
| 观点评分 | 价格分（相对环节超额收益）与论据分**双轨分存，不合并** | 0007 |
| 文档解析 | 合合信息 TextIn xParse（托管 API），取代 MinerU | 0008 |
| 向量召回 | Chroma（本地自建）做候选生成器；**时点权威仍是 PG 的 `asof` 视图** | 0009 |

## 3. 工程约定

流程细节（DoR/DoD、分支与发布、变更控制要几个人审）见 `docs/11-sdlc.md` §2、§6、§7。
下面是不看那份文档也必须守住的底线：

- **TDD**：先写失败测试再实现。**数值计算与时点逻辑必须有单测**，没有例外。
- 每个外部适配器都要有 Mock 实现与契约测试（`tests/contracts/`）。
- Prompt 版本化存 `packages/ragdemo/src/ragdemo/agents/prompts/<agent>/<version>.md`；改 prompt 必须跑评测。
- 日志结构化（JSON），必须含 `run_id`、`as_of`、`entity_id`。
- **密钥只出现在环境变量或网络代理层**。代码、日志、测试夹具、提交记录中一律不得出现。
- 数据库变更走迁移脚本（`db/migrations/`），不手工改表。
- Python 3.11+，类型注解完整，`ruff` + `mypy --strict` 在 pre-commit 与 CI 中强制。
- 代码分两个包：`packages/ragdemo-core/`（底层）与 `packages/ragdemo/`（业务层）。
  **底层不得 import 业务层**（`docs/01-architecture.md` §5），有测试守着。

## 4. 明确不做的事

不自研模型；不自建爬虫（公告走供应商接口）；不做行情终端；不做全行业覆盖；
P4 之前不做面向用户的 Web 产品前端（内部只读诊断界面除外，见 `docs/adr/0010-internal-diagnostic-web-ui-in-p1.md`）。

收到超出上述范围的需求时，先说明它在边界外，再问是否要调整边界——不要默默扩张。

---

## 5. 目录地图（进入某个目录前先读它自己的 CLAUDE.md）

每个目录都有自己的 `CLAUDE.md`，说明这个包做什么、关键模块、本包才有的硬规则、怎么测、常见坑。
它们不重复本文件，只补充本文件没有的内容。**在哪个目录改代码，先读那个目录的 CLAUDE.md。**

| 目录 | 一句话 |
|---|---|
| `packages/ragdemo-core/` | 底层：数据库连接、时点不变量检查、blob 存储；不得引用业务层 |
| `packages/ragdemo/` | 业务层总览，每个子包一行并链到各自文件 |
| `packages/ragdemo/src/ragdemo/parse/`（含 `local/`、`tiered/`） | 文档解析：生产只走合合信息，分级路由、逐页验收、本地排版（L0 关闭） |
| `…/tables/` | 表格流水线：网格、单位与期别绑定、逐格校验、引用、事实核对 |
| `…/ingest/` | 入库与重解析：抓取、解析入库、缓存重解析、网页转 PDF、CLI 入口 |
| `…/retrieval/` | 检索：BM25 加向量融合、重排、改写、期别偏置，各开关默认值与理由 |
| `…/evals/` | 评测：运行、冻结、标注导入、表格门槛、基线规则 |
| `…/adapters/`（含 `mock/`）、`embed/`、`sources/` | 外部供应商适配器与对应 Mock、嵌入、数据源 |
| `…/api/`、`dataapi/` | 只读内部诊断接口与数据接口 |
| `…/agents/`、`tools/`、`templates/`、`audit/` | P2 占位（只有提示词与约束说明），实现前须遵守 docs/07、docs/09 |
| `…/entities/`、`metrics/`、`ops/`、`quality/`、`seed/` | 实体解析、指标、告警与预算、质量检查与时点回放、种子数据 |
| `db/`（含 `migrations/`、`seed/`） | 迁移（只追加、不重命名）与种子 |
| `tests/`（含 `contracts/`） | 测试布局、标记、隔离测试库、已知失败 |
| `scripts/`、`config/`、`infra/`、`data/`、`web/`、`evals/`、`docs/` | 脚本（标明是否花钱或写库）、配置、部署、本地数据（不入库）、内部诊断前端、评测产物位置、文档地图 |

已知的目录级陷阱：`.env` 指向开发库，带 `--yes` 的命令都会写开发库；运行命令前先 `set -a; . ./.env; set +a`（CLI 不会自己读 .env）；仓库根的 `data.zip` 不要 `git add -A` 带进去。
