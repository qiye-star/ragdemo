# tests/ — 全仓库测试（底层与业务层共用一套），数据库测试用一次性临时库

根 CLAUDE.md §3 已规定 TDD、契约测试、底层不得 import 业务层（有测试守着）。本文件只讲怎么跑、怎么找。

## 这个目录里有什么

| 路径 | 用途 |
|---|---|
| `conftest.py` | 全局夹具：`temp_db`（见下）、`_no_real_model`（autouse，删掉 `MODEL_BASE_URL`，测试绝不调真实模型）；注册 `tests/corpus.py` 为插件 |
| `corpus.py` | 检索/评测共用的夹具语料（`corpus`、`as_of_2024` 夹具） |
| `test_p0_acceptance.py` / `test_p1_acceptance.py` | P0/P1 阶段验收，对应 `make accept-p0` / `make accept-p1` |
| `test_no_ingested_at_in_filters.py` | 守住 CLAUDE.md §1.1：`ingested_at` 不得进 as_of 过滤 |
| `test_snapshot_coverage.py`、`test_blob.py`、`test_config.py`、`test_cli.py`、`test_infra.py`、`test_toolchain.py` | 快照覆盖、对象存储、配置、CLI、compose/基建、工具链 |
| `adapters/` `contracts/` `dataapi/` `db/` `embed/` `entities/` `evals/` `ingest/` `metrics/` `ops/` `parse/` `quality/` `retrieval/` `seed/` `sources/` `tables/` `api/` | 按被测业务模块分目录；`parse/local`（确定性排版 L0）、`parse/tiered`（分级路由） |
| `fixtures/` | 录制的供应商响应、脱敏版面、小 PDF 等；**测试不得依赖网络**，一律读这里 |
| `contracts/` | 适配器契约（`*_contract.py` 是基类，`test_mock_*` 对 Mock 跑，真实适配器同一套契约），见 `contracts/CLAUDE.md` |

## 怎么用

```bash
make up                               # 先起 ParadeDB（宿主端口 5433，不是 5432）
set -a; . ./.env; set +a              # CLI 与脚本不会自己读 .env；pytest 夹具读 RAGDEMO_ADMIN_DSN，缺省即本地 5433
uv run pytest -v -m "not seed_full"   # 日常全量（make test 同义）
uv run pytest tests/db -m "db and not seed_full"   # 只跑数据库测试（make test-schema 会先 db check）
uv run pytest tests/retrieval -q      # 单目录
make accept-p0 ; make accept-p1       # 阶段验收
make lint ; make typecheck            # ruff（含 tests）与 mypy --strict
```

## 硬规则

- **临时库**：需要库的测试一律用 `temp_db` 夹具——用管理 DSN 建随机名空库 `t_<hex>`，结束 `DROP ... WITH (FORCE)`。**不得**连开发库/生产库做写测试。管理 DSN 用 `127.0.0.1` 而非 `localhost`（后者先解析到 `::1`，连接会卡满超时）。
- **免费 vs 付费**：测试默认全免费、离线。真实 TextIn/DeepSeek/SiliconFlow/Wind 绝不在测试里调用；供应商一律走 Mock 或 `fixtures/` 回放。需要真实服务的用例用标记 + 环境变量跳过。
- **标记**（`pyproject.toml` 里 `--strict-markers`，新标记必须先登记）：`db`（要库）、`seed_full`（要完整种子数据）、`live_engine`（要本机解析引擎容器，需 `PARSE_PADDLE_BASE_URL`，未设置自动 skip）、`local_edge`（要本机 Edge 无头渲染，`RAGDEMO_RUN_LOCAL_EDGE=1` 才跑，不进 CI）。
- 数值计算与时点逻辑必须有单测（根 CLAUDE.md §3）；每个新迁移配一个 `tests/db/test_migration_NNN.py`。
- 改 prompt/切块/检索参数还要跑三套评测（根 CLAUDE.md §1.4），单测绿不等于评测不退化。

## 常见坑

- **已知失败**：`seed_full` 标记的两条种子计数断言（实体 100、关系 200 等）在种子未录满时会失败；`make test` 已用 `-m "not seed_full"` 排除。当前是否仍失败以实跑为准（`docs/12-dev-flow.md` 的计数是历史快照）。
- 数据库没起时 `db` 类测试会连接报错，不是代码问题，先 `make up`。
- Windows 上 Git Bash 会改写 docker 命令里的 `/tmp/...` 路径（`MSYS_NO_PATHCONV=1`），测试里调 docker 要注意。
- pg_search 并行 worker 偶发崩溃已用 compose 里关闭并行规避；若测试库建索引时整库重启，先查这一条（`docs/9.28-plan/验收记录/` phase-8）。
- `tests/__pycache__` 里有 py3.11/3.12 两种 pyc，项目要求 Python 3.11+。
