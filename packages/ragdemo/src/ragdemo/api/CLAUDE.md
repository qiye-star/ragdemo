# packages/ragdemo/src/ragdemo/api — 内网只读诊断接口（FastAPI，只有 GET，adr/0010）

## 这个包做什么
给 `web/` 里的内部诊断界面提供数据：看文档与块的解析结果、版面还原、质量门禁看板、解析分档/预算、
权限隔离探针、检索四段排名并排。它是**诊断工具而不是产品接口**——不做验收结论、不生成论断文案，只展示原文块与数字。
P4 才会演化成面向用户的接口（`docs/adr/0010-internal-diagnostic-web-ui-in-p1.md`）。

## 关键模块
| 路径 | 职责 |
|---|---|
| `app.py` | `create_app(settings)`：装配路由、统一错误信封、静态挂载 `web/`（挂载必须放最后）；不含 SQL |
| `settings.py` | `from_env()`：`RAGDEMO_API_DSN`（部署，直连 app_diag 登录用户）或 `RAGDEMO_API_ALLOW_PRIVILEGED_DSN`+`RAGDEMO_DSN`（开发，连上后 `SET ROLE app_diag`）；都没有就拒绝 |
| `deps.py` | `require_as_of`（`AsOf` 依赖）、`get_conn`（每请求一连接，`read_only=on`、`statement_timeout=5000`、`timezone=UTC`） |
| `errors.py` | `ApiError` 与 `ErrorCode` 统一信封；`asof_guc_missing`/`base_table_denied` 是「约束被绕过」的运行时告警 |
| `schemas.py` | pydantic 响应模型；**模型里没有的字段会被 FastAPI 丢弃**，这是私有内容不外泄的机械保证 |
| `queries/` | SQL 与行映射，见 `queries/CLAUDE.md` |
| `routers/` | 薄 HTTP 层，见 `routers/CLAUDE.md` |
| `constants.py`、`serialize.py` | 页头横幅常量；`object` → 具体类型的窄化函数（`mypy --strict`） |

## 对外入口
- 启动：`ragdemo serve [--host 127.0.0.1] [--port 8088]`（`cli.py`）。非回环地址默认拒绝，需 `RAGDEMO_API_ALLOW_LAN=1`。
- 建库用户：`ragdemo db grant-api-read --user <名>`（口令只读 `RAGDEMO_API_DB_PASSWORD`）。
- **只有 `cli.py` 可以 import `ragdemo.api`**（`tests/api/test_sql_guards.py` 扫描守着）；`api/` 不得被其他业务包反向依赖，应能整体删除而不留痕迹。
- 本包 import：`ragdemo_core.db.session`、`ragdemo.retrieval.*`（检索诊断）、`ragdemo.parse.router`（分档判定复用）、`ragdemo.embed.mock`、`ragdemo.retrieval.models`、`ragdemo.config`、`ragdemo.seed.isolation_demo`（隔离探针用的演示身份常量）。

## 本包的硬规则
- **无写接口**：只允许 `GET`；连接层 `default_transaction_read_only=on` 是最后一道保险（`deps.py`）。
- 每个接口强制 `as_of`，无默认值、带时区；`+` 时区偏移在 query string 会被解成空格，错误信息会点名 `%2B`（`deps.py`）。
- `queries/*.py` 只查 `asof.*` 视图；非时点运维表仅限白名单 `core.parse_tier_policy`、`core.parse_retry_queue`、`quality.quality_metric`，新增须改 `tests/api/test_sql_guards.py` 的 `_NON_ASOF_ALLOWLIST`。禁止 `SELECT *`。
- `owner_user IS NOT NULL` 的块硬编码不可见，不由请求参数控制；隔离探针只返回聚合计数/布尔，不取文本列。
- 检索诊断默认用 Mock 嵌入/重排（`RAGDEMO_API_RETRIEVAL_MODELS=mock`）。切到 `siliconflow` 等于「改模型」，要先跑评测（CLAUDE.md §1.4）并产生付费调用。
- 不装 `CORSMiddleware`、不开 Swagger UI（ADR-0010 推翻条件：防止被拿去给外人看）。

## 怎么测
仓库根目录运行：
- 免费、无数据库：`pytest tests/api/test_sql_guards.py tests/api/test_settings.py tests/api/test_as_of_param.py tests/api/test_serialize.py tests/api/test_tree_builder.py tests/api/test_classify_tier.py tests/api/test_app_contract.py`
- 需要数据库（`@pytest.mark.db`）：`pytest tests/api`。`tests/api/conftest.py` 的 `api_db` 夹具在 `temp_db` 里迁移、建随机名登录用户并 `GRANT app_diag`，结束时 `DROP OWNED`/`DROP USER`；`client` 夹具给出 `TestClient`（`web_root=None`）。
- 无付费调用。

## 常见坑
- 没传 `as_of` 时返回 422 `as_of_required`，不是 503：`get_conn` 显式依赖 `AsOf`，保证校验先于建连（`deps.py` docstring）。
- Windows 上 `.js` 可能被识别为 `text/plain` 导致页面白屏，`app.py` 显式 `mimetypes.add_type`。
- `quality.dashboard` 视图没有 `computed_at <= as_of` 过滤，`queries/quality.py` 自己重写了 `DISTINCT ON`，别换回视图。
- `tests/api/test_role_grants.py` 依赖迁移里的 `app_diag` 角色授权，改授权要同步改它。
- 计划与裁决见 `docs/superpowers/plans/2026-09-22-web-diagnostic-ui.md`。
