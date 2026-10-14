# config/ — 非密钥运行配置（供应商限流、检索同义词）

## 这个目录里有什么

| 文件 | 用途 | 谁读 |
|---|---|---|
| `providers.yaml` | 每个供应商的 `base_url`、`rate_limit_qpm`、`daily_quota`、`timeout_s`、重试、`cost_per_call_cents` | `ragdemo.adapters.http`（默认路径 `config/providers.yaml`）、`adapters/siliconflow.py` |
| `synonyms.yaml` | 检索同义词表，仅 `RetrievalConfig.rewrite_enabled = true` 时生效 | `ragdemo.retrieval.rewrite` |

## 怎么用

改完直接生效于下一次进程启动；Dagster 容器把本目录只读挂到 `/app/config`，改后 `docker compose -f infra/docker-compose.yml --env-file .env restart dagster-webserver dagster-daemon`。

## 硬规则

- **密钥不在这里**——只存在于出网代理层（`infra/egress-proxy`，见 CLAUDE.md §3）。这里出现 key/token/password 一律是缺陷。
- 新增供应商同时要：适配器 + Mock + 契约测试，并在 `infra/egress-proxy/policy.yaml` 加白名单（否则出网被拒）。
- 改同义词属于改检索行为，须跑检索评测（CLAUDE.md §1.4）。

## 常见坑

- 路径是**相对当前工作目录**（`Path("config/...")`），必须在仓库根运行 CLI/脚本，换目录会找不到。
- `cost_per_call_cents` 只是估算口径，不是账单；预算开关见 `.env.example` 的 `RAGDEMO_MONTHLY_BUDGET_CNY`、`TEXTIN_*`。
