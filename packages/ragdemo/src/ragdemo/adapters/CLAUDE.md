# packages/ragdemo/src/ragdemo/adapters — 外部数据源统一适配层（公告、财务、行情、汇率、网关、嵌入客户端）

## 这个包做什么
所有「向包外世界取数」的代码都在这里：把各供应商的原始响应包成统一的 `RawResponse`，
再归一化为 `FactRecord` / `NormalizedDocument` / 价格行等。每个适配器负责回答「这条数据现实中最早何时可获知」
（`known_at()`），因为只有它懂原始字段语义（Tushare 取 `ann_date` 不取 `end_date`，EDGAR 取 `acceptanceDateTime`）。
适配器**不写库**：落 `provider_snapshot`、写 `core.*` 在 `ingest/` 里完成。

## 关键模块
| 文件 | 职责 |
|---|---|
| `base.py` | `Adapter`/`FactAdapter` Protocol、`FetchContext`、`RawResponse`、`FactRecord`（构造时校验 known_at 带时区、不在未来、不早于 period_end）、`require_aware` |
| `announcements.py` | 公告侧归一化模型 `NormalizedDocument`/`NormalizedBlock` 与 `AnnouncementProvider` Protocol（adr/0005） |
| `http.py` | `HttpClient`（限流 `TokenBucket`、仅幂等重试、配额、白名单响应头）与 `ProviderConfig.load`（读 `config/providers.yaml`） |
| `errors.py` | `AdapterError` 家族：`RateLimited` / `QuotaExceeded`（不重试）/ `UpstreamUnavailable` / `ProviderRefused` |
| `secrets.py` | `strip_secrets`：剥离参数里的 token/key 等（由 `ingest/snapshot.py` 在落快照前调用） |
| `mcp_gateway.py` | MCP 网关客户端（Wind/iFind/Tushare 的唯一出口，网关持有上游凭证）；`gateway_client_from_env` |
| `cninfo.py` / `hkexnews.py` / `edgar.py` | 巨潮 / 港交所 / SEC 公告适配器；**刻意不实现 `AnnouncementProvider`**（先列表后下载全文两次调用），由 `ingest/*_docs.py` 配套使用 |
| `ifind_notice.py` | iFind 公告降级层，只给标题/日期/片段，`has_full_text` 恒 False |
| `local_manifest.py` | 本地 `manifest.jsonl` → `NormalizedDocument`（也不实现 Provider） |
| `tushare.py` / `wind.py` / `wind_kline.py` / `wind_structured.py` / `cfets.py` | 行情、财务三表、K 线降级、股东/主营构成、人民币中间价 |
| `snapshot_replay.py` | 用已留存的快照回放 Wind 网关，零真实调用 |
| `html_render.py` | `HtmlRenderer` Protocol + 本机 Edge 无头实现（EDGAR 8-K 转 PDF） |
| `siliconflow.py` | 仅 `client_from_config`：把 providers.yaml 与传入 base_url 拼成 `HttpClient`，嵌入/重排协议映射在 `embed/`、`retrieval/` |
| `mock/` | 各 Protocol 的 Mock 实现，见 `mock/CLAUDE.md` |

## 对外入口
- 调用方：`ingest/*`（Dagster 资产与 CLI）、`embed/siliconflow.py`、`retrieval/`。业务代码只依赖这里的 Protocol 与归一化模型。
- 本包 import：`ragdemo_core.blob`（`local_manifest.py`）。**有三处反向依赖需知**：`announcements.py` import `ragdemo.tables.grid`，
  `ifind_notice.py` import `ragdemo.ingest.announcement_index`，`wind_structured.py` import `ragdemo.ingest.period_disclosure`；不要再新增，也不要在这些被引模块里 import `adapters` 造成环。
- 不得 import `psycopg` 写库（`snapshot_replay.py` 只读快照是唯一例外）。

## 本包的硬规则
- 新数据源 = 实现 `Adapter`（含 `known_at`）+ Mock + 契约测试；`known_at` 取现实可获知时刻，不取拉取时间（CLAUDE.md §1.1、docs/03-point-in-time.md §1.3）。日期精度的数据（如巨潮 `announcementTime`、本地 manifest）取当日 `23:59:59+08:00`，宁晚勿早。
- 所有出网走 `HttpClient`，不得直接 `httpx.get`；`HttpClient` 把 httpx 日志降到 WARNING 以免泄漏 query 参数，勿动。
- 密钥不进代码：Tushare token 由出网代理注入，SiliconFlow 密钥只在 `infra/egress-proxy`，MCP 网关地址只来自 `RAGDEMO_MCP_GATEWAY_URL`（无默认值；明文 HTTP 发往公网须显式 `RAGDEMO_MCP_GATEWAY_ALLOW_INSECURE`）。
- 凡真实取数点必须在调用方留存快照：`tests/test_snapshot_coverage.py` 扫描 `adapters/` 之外的函数——调用 `fetch`/`list_documents`/`fetch_full_text`/`call_tool` 的函数必须同时调 `record_snapshot` 等（CLAUDE.md §1.5）。
- 重试耗尽必须抛 `UpstreamUnavailable`，不得返回部分数据；`QuotaExceeded` 不重试。
- Wind 等「自然语言」接口的响应一律先校验再入库（累计 vs 单季、单位、披露日），校验不过整份拒绝（见各模块 docstring 的「实测踩坑」）。

## 怎么测
仓库根目录运行，均免费、无数据库、无网络（用夹具 `tests/fixtures/{wind,tushare,cfets,edgar,hkexnews,ifind,...}`）：
- `pytest tests/adapters tests/contracts`
- `tests/contracts/adapter_contract.py` 等是可复用契约基类，`test_mock_*.py` / `test_wind_contract.py` / `test_pg_dataapi.py` 把它们套到具体实现。
- `tests/contracts/test_edge_html_renderer.py` 默认跳过（`local_edge` 标记，需 `RAGDEMO_RUN_LOCAL_EDGE=1` 与本机 Edge）。
- `pytest tests/test_snapshot_coverage.py`、`tests/test_no_ingested_at_in_filters.py` 是静态守卫。

## 常见坑
- `FactRecord`/`RawResponse` 构造即校验，回填历史分区时若 `known_at` 早于 `period_end` 会直接抛 `ValueError`，不是静默放过。
- MCP 网关：成功是 SSE 帧、失败可能是裸 JSON；HTTP 500 可能是请求本身不合法（不应重试）；`/mcp/` 尾斜杠不能省（`mcp_gateway.py` 模块 docstring）。
- Tushare 价格 `fetch()` 产出的 payload 是「已展开行字典列表」，不是原始 fields/items。
- Wind 返回当前值、不给披露日，`known_at` 由调用方按公告目录回填（`ingest/period_disclosure.py`）。
- `providers.yaml` 里的 `base_url` 对 siliconflow/deepseek 只是限流与配额参数来源，实际地址来自环境变量指向回环代理。
