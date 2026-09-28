# 阶段 0 验收记录 · 环境与基线

- 日期：2026-09-28
- 依据：`docs/9.28-plan/2026-09-28-数据底座完善总计划.md` 阶段 0
- 起点提交：`5df457e`（分支 `p0-foundation`），本阶段改动尚未提交

## 结论

**出口验收两项全部通过。** 任务 0.2 与 0.5 中需要你本人或创始人完成的部分未完成，
列在文末"遗留事项"；它们都不是阶段 0 的出口条件，也不阻塞阶段 1（阶段 1 不需要任何外部凭证）。

| 出口验收 | 结果 |
|---|---|
| `make lint && make typecheck && make test` 全绿 | ✅ |
| `phase-0.md` 含基线数字与凭证就位情况 | ✅（本文件） |

---

## 任务逐项

### 0.1 启动容器，`make test` 全绿 — ✅

`make up` 后三个容器健康：

```
ragdemo-egress   Up 42 hours
ragdemo-db       Up (healthy)
ragdemo-chroma   Up (healthy)
```

`make test`：

```
==== 843 passed, 1 skipped, 4 deselected, 5 warnings in 190.68s (0:03:10) =====
```

- 4 deselected：带 `seed_full` 标记的 P0 计数断言（实体 100 / 关系 200 等），按设计在
  `make test` 中排除，数据补齐后由 `make accept-p0` 验证。
- 1 skipped：`tests/test_p1_acceptance.py:235` 的 7 天双跑一致性测试。
  基线快照写于 `2026-09-21T14:11:17Z`，审计时只满 6 天；**北京时间 2026-09-28 22:11 之后**
  重跑即可真正断言。该项属于阶段 4 的 4.5，不在本阶段门禁内。
- 5 warnings：pytest 在 Windows 上清理临时目录失败（`WinError 145`），与代码无关。

### 0.2 凭证 — ⚠️ 部分（需要你操作）

我无法替你作废或生成供应商 key，也看不到硅基流动后台。下表只记"已配/未配"，不记任何值。

出网代理（`ragdemo-egress` 容器环境变量）：

| 变量 | 状态 | 用途 / 影响阶段 |
|---|---|---|
| `TEXTIN_APP_ID` / `TEXTIN_SECRET_CODE` | 已配 | 文档解析（阶段 3） |
| `TUSHARE_TOKEN` | **未配** | A 股财务与行情（阶段 2） |
| `MCP_GATEWAY_TOKEN` | **未配** | MCP 网关，与 Tushare 二选一（阶段 2） |
| `MODEL_API_KEY` | **未配** | 真实表格描述器（阶段 3） |
| `EMBEDDING_API_KEY` | **未配** | 真实嵌入/重排（阶段 5） |

宿主机 `.env`（连接地址与参数，非密钥）：

| 变量 | 状态 |
|---|---|
| `TEXTIN_BASE_URL` / `TEXTIN_MAX_PAGES_PER_RUN` | 已配 |
| `SILICONFLOW_BASE_URL` | 未配 |
| `RAGDEMO_MCP_GATEWAY_URL` | 未配 |
| `TEXTIN_COST_PER_PAGE_CNY` | 未配（阶段 1.6 需要） |

**硅基流动旧 key**（2026-09-23 前后在对话中明文出现过的那一个）：是否已在后台作废，**我无法验证，需要你确认**。
代理里 `EMBEDDING_API_KEY` 目前为空，所以旧 key 至少没有被配进本系统。
配置新凭证后，必须经 `make up`（带 `--env-file .env`）重建代理容器，否则代理读不到——9-23 试跑踩过这个坑。

### 0.3 基线数字 — ✅

实库（`ragdemo-db`）行数：

| 表 | 行数 | 表 | 行数 |
|---|---|---|---|
| `core.entity` | 18 | `core.fin_fact` | **0** |
| `core.entity_alias` | 73 | `core.price_daily` | 5 |
| `core.entity_relation` | 30 | `core.provider_snapshot` | **0** |
| `core.entity_node_membership` | 28 | `core.document` | 7 |
| `core.node_metric` | 30 | `core.doc_block` | 288 |
| `core.metric_source_map` | 0 | `core.embedding_cache` | 194 |
| `core.ai_revenue_rule` | 0 | `core.parse_retry_queue` | 0 |
| `core.propagation_rule` | 15 | `core.entity_resolution_queue` | 0 |
| `core.event` / `opinion` / `opinion_score` | 0 / 0 / 0 | `quality.quality_metric` | 8 |
| `evals.eval_retrieval` / `eval_extraction` / `eval_judgement` / `eval_run` | 0 / 0 / 0 / 0 | `audit.tool_call_log` | 0 |

文档按来源：

| source | parse_engine | 份数 |
|---|---|---|
| cninfo | `textin:3.29.3+13c878f342724768` | 5 |
| edgar | `vendor:edgar` | 1 |
| mock-announcements | `vendor:mock-announcements` | 1 |

块与向量：

| 指标 | 数值 |
|---|---|
| 叶子块 | 224 |
| 叶子块有 bbox | 221（98.7%） |
| 表格块 / 表格 `content_desc` 非空 | 28 / 28 |
| 叶子块有向量 | 217 |
| 叶子块 `embedding_version` 非空 | **0**（224 条全为 NULL） |
| `embedding_cache.model` 分布 | 仅 `mock-1024`（194 行） |

行情：5 个实体、仅 `2024-10-28` 一个交易日，`source = tushare`。

扩展：`pg_search 0.25.9`、`vector 0.8.4`、`pg_trgm 1.6`、`pg_ivm 1.13` 等均已安装。

测试：843 passed / 1 skipped / 4 deselected；mypy 检查 218 个源文件；ruff 检查 219 个文件。

**新发现（记入后续阶段，本阶段不修）**：217 个已嵌入的叶子块 `embedding_version` 全是 NULL。
现在的 `embed/batch.py:183` 会在写向量的同一条 UPDATE 里写 `embedding_version`，但这是
提交 `75e1312`（2026-09-22）才加上的；这批块的入库时间是 2026-09-22 03:11–03:12 UTC，
向量是在那次提交之前、由不写版本号的旧代码写入的。
影响：阶段 5 切真实嵌入时，无法用 `embedding_version` 区分新旧向量。**处理：阶段 1 补一条校验，
阶段 5 重嵌入前先把这 217 条标记为 `mock-1024` 或直接清空重嵌。**

### 0.4 `uv.lock` 镜像地址改写 — ✅ 根治

**根因**：本机用户级配置 `%APPDATA%\uv\uv.toml` 把清华镜像设成了 `default = true` 的索引；
uv 会把解析时所用的索引地址写进 `uv.lock`，于是每次 `uv run` 都把整份锁文件的源地址改成镜像
（2880 行增 / 2880 行删，零实质变更）。

**修复**：根目录 `pyproject.toml` 增加项目级索引，钉住官方 PyPI。项目级配置优先于用户级配置：

```toml
[[tool.uv.index]]
name = "pypi"
url = "https://pypi.org/simple"
default = true
```

**验证**：

```
$ uv lock          → Resolved 141 packages in 1ms
$ git diff --stat uv.lock   → （无输出，零改动）
$ uv run python -c "print('ok')"  → ok
$ git diff --stat uv.lock   → （无输出，零改动）
```

后续 `make lint/typecheck/test` 全程 `uv run`，结束后 `uv.lock` 仍无改动。
本机访问 `pypi.org` 实测 200 / 0.6s，与镜像（0.7s）相当，钉官方索引不影响本机安装速度。
你本机的用户级镜像配置没有动，其他项目照常使用镜像。

### 0.5 创始人录入 — ⏳ 未开始（不是工程任务）

| 项 | 当前 | 目标 |
|---|---|---|
| 实体 | 18 | 100 |
| 别名 | 73 | ~400 |
| 关系 | 30 | 200 |
| 检索评测用例 | 0 | 100 |

录入约定见 `db/seed/README.md`。实体/关系阻塞阶段 6，评测用例阻塞阶段 5；请尽早启动。

---

## 为达到"全绿"所做的改动

门禁原本不是全绿：`ruff format --check` 有 12 个文件格式漂移，`mypy` 有 36 个错误。

| 改动 | 文件 | 性质 |
|---|---|---|
| 格式化 12 个漂移文件（只格式化这 12 个，逐个点名，未用目录通配） | `evals/cli.py`、`evals/sampler.py`、`ingest/assets_docs.py`、`ingest/cli.py`、`parse/router.py`，以及 `tests/` 下 7 个 | 纯格式 |
| 36 个 mypy 错误：测试直接调用 Dagster `@asset_check`，Dagster 把 `AssetChecksDefinition.__call__` 的返回类型标成 `object`。新增 `_run()` 帮助函数，用 `isinstance(result, AssetCheckResult)` 断言并收窄类型，21 处调用改用它 | `tests/quality/test_checks.py` | 类型 + 一条真实的运行时断言 |
| `_docs()` 原本标成 `list[object]`，再用 37 个 `type: ignore` 和 4 个 `cast` 兜底；改成真实类型 `list[NormalizedDocument]`，删掉全部无用的 ignore 与 cast | `tests/ingest/test_documents.py` | 纯类型 |
| 直接调用 `@asset` 缺 `cast`，按该文件自己第 61、128 行的既有写法补上 | `tests/ingest/test_assets_docs.py` | 纯类型 |
| 钉住官方 PyPI 索引 | `pyproject.toml` | 配置 |

没有修改任何断言，没有修改任何业务代码的行为。改动后三道门：

```
$ make lint       → All checks passed! / 219 files already formatted
$ make typecheck  → Success: no issues found in 218 source files
$ make test       → 843 passed, 1 skipped, 4 deselected
```

---

## 遗留事项（带入后续阶段，不阻塞阶段 1）

| # | 事项 | 负责 | 阻塞 |
|---|---|---|---|
| 1 | 确认硅基流动旧 key 已在后台作废，生成新 key | 你 | 阶段 5 |
| 2 | 提供 Tushare 令牌，或 MCP 网关地址与令牌（二选一） | 你 | 阶段 2 |
| 3 | 提供国内生成模型 key（DeepSeek 等） | 你 | 阶段 3 |
| 4 | 提供 TextIn 单页价格 `TEXTIN_COST_PER_PAGE_CNY` | 你 / 创始人 | 阶段 1.6 |
| 5 | 启动实体、别名、关系、评测用例录入 | 创始人 | 阶段 5、6 |
| 6 | 北京时间 09-28 22:11 后重跑 7 天双跑一致性测试 | 工程 | 阶段 4.5 |
| 7 | 217 个叶子块 `embedding_version` 为空 | 工程 | 阶段 1 / 5 |
| 8 | 本阶段改动尚未提交，等你确认后提交 | 你 | — |
