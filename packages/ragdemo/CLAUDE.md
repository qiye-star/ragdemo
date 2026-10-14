# packages/ragdemo — 业务层：接入、解析、检索、评测、数据接口、诊断 API 与 CLI

## 这个包做什么
整条链路 ingest → parse → store → retrieve → agents/API 的业务代码都在 `src/ragdemo/`。它依赖底层包 `ragdemo-core`
（迁移、`as_of_session`、不变量、`BlobStore`，见 `../ragdemo-core/CLAUDE.md`），底层**不得**反向 import 本包。
入口是 `ragdemo` 命令（`pyproject.toml` 的 `[project.scripts]` → `ragdemo.cli:main`）。
现状：P0/P1 与数据底座已落地；`agents/`、`tools/`、`templates/`、`audit/` 是 P2 占位（只有 docstring，无实现）。

## 子包地图（每行一个子包；细节见各自 CLAUDE.md，路径均相对 `src/ragdemo/`）
| 子包 | 一句话 | 文档 |
|---|---|---|
| `adapters/` | 外部数据源统一适配层（含 `mock/`），出网都走 `HttpClient` | `adapters/CLAUDE.md` |
| `sources/` | 数据源分工表、分层降级路由、冷却、待补清单（ADR-0012） | `sources/CLAUDE.md` |
| `ingest/` | Dagster 资产/分区/调度、时点写入中间件、`ragdemo docs`/`facts` CLI | `ingest/CLAUDE.md` |
| `parse/` | 文档解析与切块（TextIn、本地 L0、分档路由） | `parse/CLAUDE.md` |
| `tables/` | 表格网格、核验、引用、回填 | `tables/CLAUDE.md` |
| `embed/` | 1024 维嵌入、内容哈希缓存、批量嵌入、向量索引对账 | `embed/CLAUDE.md` |
| `retrieval/` | 过滤 → BM25+向量 → RRF → 重排 → 父子块展开 | `retrieval/CLAUDE.md` |
| `evals/` | 三套评测集的指标与运行（`ragdemo eval`） | `evals/CLAUDE.md` |
| `entities/` | 实体解析三层降级（目前仅测试在用） | `entities/CLAUDE.md` |
| `metrics/` | 口径版本化、Excel 导入、派生值（同比/环比/TTM）、汇率换算 | `metrics/CLAUDE.md` |
| `dataapi/` | 给 Agent 的只读 `DataApi` 契约 + Mock/PG 实现 | `dataapi/CLAUDE.md` |
| `quality/` | 质量门禁、月度预算、时点泄漏抽样重放 | `quality/CLAUDE.md` |
| `ops/` | 告警出口与质量告警 | `ops/CLAUDE.md` |
| `seed/` | 种子 CSV 生成与导入、隔离演示种子 | `seed/CLAUDE.md` |
| `api/` | 内网只读诊断 FastAPI（只有 GET） | `api/CLAUDE.md` |
| `agents/` `tools/` `templates/` `audit/` | P2 占位；`agents/prompts/` 现存解析用 prompt | 各自 `CLAUDE.md` |

顶层单文件：`cli.py`（`ragdemo` 命令组装：`db migrate/seed/check/replay-check/...`、`serve`，并挂载 `docs`/`facts`/`eval`/`metrics` 组）、
`config.py`（`RagdemoConfig`，从环境变量读运行配置，缺必填项大声失败，`TEXTIN_*`、`RAGDEMO_BLOB_ROOT`、`RAGDEMO_MONTHLY_BUDGET_CNY` 等）。

## 依赖方向
设计图见 `docs/01-architecture.md` §5：`api/cli → agents → tools → retrieval/metrics/entities → ingest → adapters → 数据库`，不得反向或横向跨层。
**代码与该文档有出入，新增代码时不要再扩大**：
- `adapters/` 本应不 import 任何其他子包，实际有三处：`announcements.py`→`tables.grid`、`ifind_notice.py`→`ingest.announcement_index`、`wind_structured.py`→`ingest.period_disclosure`。
- `quality/checks.py` 依赖 `ingest`、`parse`；`metrics/manual.py` 依赖 `ingest.writer`/`snapshot`；`metrics/derived.py` 与 `dataapi/` 互相引用类型。
- `api/` 只能被 `cli.py` import（`tests/api/test_sql_guards.py` 守着）。
- 守卫测试：`tests/test_toolchain.py`（底层不 import 业务层）、`tests/test_no_ingested_at_in_filters.py`（`ingested_at` 不进比较）、`tests/test_snapshot_coverage.py`（取数点必须留快照）。

## 本包的硬规则
- 业务代码不得 import 供应商 SDK，只经 `adapters/` 的 Protocol（CLAUDE.md §1.5）；读 `asof.*` 视图一律包在 `as_of_session` 里。
- 每个子包新增外部适配器 = 适配器 + Mock + `tests/contracts/` 契约测试。
- 含 Dagster `@asset`/`@asset_check` 的文件不要加 `from __future__ import annotations`（`ingest/assets.py`、`quality/checks.py`、`quality/budget_asset.py`）。
- 新增依赖写进 `pyproject.toml`；注释里已记载选型理由（如不用 AGPL 的 PyMuPDF，用 `pdfplumber`）。

## 怎么测
仓库根目录运行（迁移路径与种子路径都是相对 cwd 的 `db/...`）：
- 全量：`pytest`（`--strict-markers`，标记：`db`、`seed_full`、`live_engine`、`local_edge`）。
- 免费且无需数据库：`pytest -m "not db"`。需要数据库的测试连 `RAGDEMO_ADMIN_DSN`（默认 `postgresql://postgres:ragdemo@127.0.0.1:5433/postgres`），`temp_db` 夹具每个测试建/删随机空库。
- `live_engine`（本机解析引擎容器）、`local_edge`（本机 Edge）默认不进 CI。付费/真实外部调用只发生在显式 `--models siliconflow`、真实 TextIn、真实 Wind/Tushare 网关等路径，测试默认全部 Mock。
- 提 PR 前：`ruff` + `mypy --strict`；改 prompt/切块/检索/模型要跑评测并贴数字（CLAUDE.md §1.4）。

## 常见坑
- 在 Windows 下运行：用 `127.0.0.1` 而非 `localhost`；`.js` MIME 见 `api/CLAUDE.md`。
- 仓库里 `docs/` 对子包的路径简写（如 `src/agents/...`）省略了 `packages/ragdemo/src/ragdemo/` 前缀。
