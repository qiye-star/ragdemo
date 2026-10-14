# packages/ragdemo/src/ragdemo/api/routers — 诊断接口的 HTTP 路由（薄层，仅 GET）

## 这个包做什么
把 `api/queries/*` 接到 `/api/...` 路径上：解析参数、注入 `AsOf` 与 `ConnDep`、调用查询函数、声明 `response_model`。不写 SQL、不含业务判断。

## 关键模块
| 文件 | 路径 |
|---|---|
| `meta.py` | `GET /api/meta`：连接身份体检（是否仍是超级用户）+ 当前 as_of 下可见行数与 `known_at` 范围 |
| `documents.py` | `/api/documents`、`/api/documents/{doc_id}`、`.../pages/{page}/layout` |
| `blocks.py` | `/api/documents/{doc_id}/blocks`、`.../tree`、`/api/blocks/{block_id}` |
| `quality.py` | `/api/quality/registry`、`/dashboard`、`/metrics/{metric}/history` |
| `parse_ops.py` | `/api/parse/tiers|policies|budget|retry-queue|warnings` |
| `isolation.py` | `/api/isolation/probes`、`/catalog`（不接受身份/表名参数） |
| `retrieval.py` | `/api/retrieval/search` |

## 对外入口
仅由 `api/app.py` 以 `prefix="/api"` 挂载。新增路由要在 `app.py` 的 `include_router` 列表里加一行。

## 本包的硬规则
- 只用 `@router.get`；不得新增 POST/PUT/DELETE（ADR-0010）。
- 路由签名必须经 `AsOf`/`ConnDep`，不得自己建连接或绕开 `as_of` 校验。
- 响应必须声明 `response_model`，不返回裸 dict，否则失去「模型外字段被丢弃」的保护。
- 不接受会改变可见性的参数（身份、租户、表名）。

## 怎么测
`pytest tests/api`（`test_app_contract.py` 检查全局契约（免费无库），各 `test_*_api.py` 按路由；需数据库，夹具见上级 `api/CLAUDE.md`）。

## 常见坑
- `/api/retrieval/search` 默认用 Mock 嵌入与重排，不产生外部调用；切模型的限制见上级文档。
