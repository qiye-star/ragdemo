# packages/ragdemo/src/ragdemo/entities — 实体解析三层降级（代码 → 别名 → 上下文），不确定就进人工队列

## 这个包做什么
把供应商代码或名称解析成 `core.entity.entity_id`，并给出置信度。三层降级：
代码精确匹配 → 别名唯一匹配 → 多别名命中时用上下文（共现实体）消歧；仍不确定就**不猜**，写入 `core.entity_resolution_queue` 等人工（`docs/04-ingestion.md` §4）。

## 关键模块
| 文件 | 职责 |
|---|---|
| `resolver.py` | `EntityResolver`：`resolve_code`、`resolve_name`（别名 + 上下文消歧 + 模糊候选）、`enqueue_unresolved`；返回 `Resolution`/`Candidate` |

## 对外入口
- 现状：**除测试外没有任何业务代码调用 `EntityResolver`**。`ingest/documents.py` 对查不到实体只写警告日志、照常入库，并在 docstring 里说明接线方式（同步入队还是下游补扫）尚待决定。接线前先读那段说明。
- 本包只依赖 `psycopg`；可直接读 `core.entity`、`core.entity_alias`（参考表，非时点表）。

## 本包的硬规则
- 模糊匹配（`pg_trgm` 的 `similarity`）只产出候选供人工参考，**绝不自动采纳**（`resolve_name` 的 `fuzzy` 分支返回 `entity_id=None`、置信度 `low`）。例：「中兴新材」不得被自动匹配成「中兴通讯」。
- `enqueue_unresolved` 对已解析的实体抛 `ValueError`；队列长度是数据质量的监控指标。
- 别名多命中且上下文也不能唯一确定时，返回 `alias_ambiguous` 而不是任选其一。

## 怎么测
`pytest tests/entities`（`test_resolver.py`，使用数据库，临时库夹具 `temp_db`）。免费。

## 常见坑
- 表 `core.entity_resolution_queue` 在迁移 `005_opinions_evals_audit.sql`；`pg_trgm` 扩展需已启用（模糊候选依赖它）。
- 供应商代码列：`tushare_code`/`ifind_code`/`wind_code`/`edgar_cik`，新增数据源要同时加列与 `resolve_code` 的 WHERE。
