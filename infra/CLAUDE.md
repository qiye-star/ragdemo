# infra/ — docker-compose 全栈、出网代理、Dagster 镜像、自托管解析引擎

自建 ParadeDB 单库 + 本地 Chroma，不用托管 RDS（ADR-0002/0009）。所有服务端口只绑 `127.0.0.1`。

## 这个目录里有什么

| 路径 | 用途 |
|---|---|
| `docker-compose.yml` | 全栈定义（下表）；用 `make up/down/clean`（自带 `--env-file .env`） |
| `egress-proxy/` | mitmproxy：`policy.yaml` 出网白名单（默认 deny）+ `addon.py` 凭据注入；**唯一持有供应商密钥的容器** |
| `parse-engines/` | 自托管 PaddleOCR：`paddle/Dockerfile`、`paddle/server.py`、`common/shim.py`（stdlib HTTP 薄壳，单线程串行，不记录请求体） |
| `dagster/` | Dagster 镜像与 `dagster.yaml`/`workspace.yaml`/`entrypoint.sh`（把 .env 里的 127.0.0.1 改写为服务名 `egress-proxy`） |
| `backfill.md` | 阶段二回填命令、顺序、限速（默认 dry run，加 `--yes` 才执行） |
| `runbook-backup.md` | 备份与恢复手册（PG 与 Chroma 必须同一 STAMP 配对） |

服务与宿主端口（均回环）：

| 服务（容器） | 端口 | 备注 |
|---|---|---|
| `paradedb`（ragdemo-db，`paradedb/paradedb:0.25.9-pg18`） | 5433→5432 | 并行 worker 已关闭（pg_search 会崩） |
| `chroma`（ragdemo-chroma，`chromadb/chroma:1.5.9`） | 8001→8000 | 关遥测；仅本地自建，禁 Chroma Cloud |
| `egress-proxy`（ragdemo-egress） | 8080 正向；8081 TextIn、8082 SiliconFlow、8083 Tushare、8084 Wind、8085 iFinD、8086 DeepSeek（反向） | 业务只知 `127.0.0.1:<端口>`，凭据由 addon 注入 |
| `parse-paddle` | 无宿主端口 | 仅 `parse-internal` 内部网络，无出网；内存上限 `PARSE_MEM`（默认 13g），并发必须为 1 |
| `parse-gateway`（socat） | 8091 | 无凭据，唯一同时在两个网络的容器 |
| `dagster-webserver` / `dagster-daemon` | 3000 | 运行状态存 PG 的 `dagster` schema（迁移 024）；开发态只读挂载 `packages/` 与 `config/` |

## 怎么用

```bash
cp .env.example .env            # 填本地值；.env 被忽略
make up                         # = docker compose -f infra/docker-compose.yml --env-file .env up -d --wait
make down                       # 停；make clean 会 down -v，**删卷丢库**
# 改了密钥：只重建 egress 容器
docker compose -f infra/docker-compose.yml --env-file .env up -d --force-recreate egress-proxy
```

## 硬规则

- **密钥只通过 `.env`/环境变量进入 egress-proxy**，`policy.yaml`、compose、Dockerfile 里不写任何真实密钥；只有 egress-proxy 的 `environment` 里列密钥名，其它服务一律不得加。Dagster/业务容器里没有密钥。
- 新供应商 = 在 `policy.yaml` 加白名单（不在名单即拒绝）；`policy.yaml` 里不写部署特定的内网网关地址（见文件内注释）。
- 解析引擎容器必须留在 `internal` 网络：文档只进不出（CLAUDE.md §0）。
- 镜像版本固定，不用 `latest`；升级 ParadeDB 必须重跑 P0 全部测试。
- 不要把 egress 日志/脱敏前的 URL 查询串放进仓库（Tushare MCP 的 token 在查询串里，所以 `flow_detail=0`）。

## 常见坑

- 必须带 `--env-file .env`（根目录）：compose 默认读 `infra/.env`，否则 `TEXTIN_*` 等变量静默为空。
- 代理只在容器启动时读 `.env`，改密钥后要重建 egress 容器。
- 8081–8086 是回环明文反代，不是生产形态（生产用正向代理 8080 + 导出 CA）。
- Windows 上 Dagster 镜像重建很慢（约 366 MB），开发态靠挂载 + restart 即可。
- `backfill.md` 里「迁移当前到 023」已过期（现为 034）。
- `.env.example` 才是变量清单的权威；改 compose 变量要同步它。
