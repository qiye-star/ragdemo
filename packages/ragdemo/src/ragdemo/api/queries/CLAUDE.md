# packages/ragdemo/src/ragdemo/api/queries — 诊断接口的 SQL 与行映射（不依赖 fastapi）

## 这个包做什么
`api/` 里**所有手写 SQL** 都在这里，返回 `api/schemas.py` 的模型或纯数据；不 import fastapi，可以脱离 HTTP 在 REPL/单测里跑。

## 关键模块
| 文件 | 职责 |
|---|---|
| `documents.py` | 文档列表/详情、页版面还原 |
| `blocks.py` | 块列表、块详情、`build_tree`（纯函数，不碰连接） |
| `retrieval.py` | `run_search`：包装 `RetrievalService`，把 BM25/向量/融合/重排/最终并排；内带结果缓存（`clear_cache`） |
| `isolation.py` | 隔离探针：身份来自硬编码 `PROBES`，只返回聚合计数与布尔 |
| `parse_ops.py` | 分档分布、策略、月度预算、重试队列、告警；复用 `ragdemo.parse.router` 的谓词与算法，不重写 |
| `quality.py` | 质量看板（`quality.quality_metric`，自己做 `computed_at <= as_of` 过滤） |

## 对外入口
仅被 `api/routers/*` 调用。可 import `ragdemo_core.db.session`、`ragdemo.retrieval.*`、`ragdemo.parse.router`；不得 import fastapi。

## 本包的硬规则
- 读时点数据必须在 `as_of_session` 内、只查 `asof.*` 视图；唯一豁免是 `isolation.py` 故意查 `core.doc_block` 验证数据库会 42501 拒绝（`tests/api/test_sql_guards.py::_ALLOWED_BASE_TABLE_HITS`）。
- 非 asof 的运维表只能是白名单三张，且 `parse_ops.py`/`quality.py` 仍要做时点形状过滤或明说原因。
- 禁止 `SELECT *`；不得多选响应模型里没有的文本列（尤其 `content`）。
- `retrieval.py` 复用的 `retrieval.*` 内部查询不在 `test_sql_guards` 扫描范围内，靠这些模块自身只查 asof 视图来保证。

## 怎么测
`pytest tests/api`（多数需数据库，夹具见上级 `api/CLAUDE.md`）；`test_sql_guards.py`、`test_tree_builder.py`、`test_classify_tier.py`、`test_app_contract.py` 免费无库。

## 常见坑
- 连接是 `Connection[tuple[object, ...]]`，取值用 `api/serialize.py` 的 `as_int`/`as_str` 等窄化，不要散落 `# type: ignore`。
- 检索诊断的请求超时与事务边界见 `retrieval.py` 模块 docstring，改之前先读。
