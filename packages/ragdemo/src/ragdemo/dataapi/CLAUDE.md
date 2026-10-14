# packages/ragdemo/src/ragdemo/dataapi — 给 Agent 用的只读数据接口（契约 v1）与其 Mock / PG 实现

## 这个包做什么
Agent 层（P2）读数据的唯一正门：一个 `DataApi` Protocol，15 个只读方法（实体解析、事实/派生值、行情、检索、原文块、公告、
股东、分部、汇率、关系/传导规则）。所有方法强制 `as_of`、只读 `asof.*` 视图、每个数值带溯源对象。
有两份实现——`MockDataApi`（内存）与 `PgDataApi`（数据库）——跑同一套契约测试，行为必须一致，让 Agent 开发不必等真实数据。

## 关键模块
| 文件 | 职责 |
|---|---|
| `contract.py` | `DataApi` Protocol、全部返回类型（`FactValue`、`DerivedValue`、`BlockHit`…）、溯源类型（`FactCitation`/`BlockCitation`/`CellCitation`/`RecordCitation`）、`require_as_of` |
| `pg.py` | `PgDataApi`：每个方法第一行 `require_as_of`，随后 `as_of_session`；`search_blocks` 委托注入的 `retrieval.service.RetrievalService` |
| `mock.py` | `MockDataApi`：默认装载 `sample.SAMPLE`；检索只是朴素关键词计数，不模拟真实排序质量 |
| `sample.py` | 契约测试与 Mock 共用的**示意**数据集（不是真实财务数字），覆盖更正、口径改版、未来文档、私有材料、`can_show_raw=false`、停用规则 |
| `sample_seed.py` | `seed_sample()`：把 `SAMPLE` 直接 INSERT 进已迁移的临时库（只用于测试/演示，禁对生产库调用） |
| `calendar.py` | `last_trading_day`：交易日历从 `asof.price_daily` 推导，不引入日历数据源 |

## 对外入口
- 使用方：P2 Agent/工具层（尚未建）、`tests/contracts/test_mock_dataapi.py`、`test_pg_dataapi.py`；`metrics/derived.py` 与 `tables/citation.py` 反向 import 本包的类型/`require_as_of`。
- `api/`（内网诊断接口）**不走** `DataApi`，它有自己的 `api/queries/`。
- 本包 import：`ragdemo_core.db.session`、`retrieval.service/types`（仅 `pg.py`）、`metrics.derived`（派生计算，CLAUDE.md §1.2）、`embed.mock`（仅 `sample_seed.py`）。
- 使用指南见 `docs/13-agent-integration.md`（A 面）；契约文档 `docs/9.28-plan/数据接口契约-v1.md`，**契约以 `tests/contracts/dataapi_contract.py` 为准**。

## 本包的硬规则
- `as_of` 是仅限关键字参数、必须带时区、无默认值；新增方法必须同样做，并在 `contract.py` 的 `DataApi`、`mock.py`、`pg.py` 三处同时实现（CLAUDE.md §1.1）。
- 只读 `asof.*` 视图与参考表，不得查 `core.*` 基表（`calendar.py` 里 `JOIN core.entity` 是参考表，不是时点表）。
- 数值一律 `Decimal`，不用 float；查不到返回 `None`/空列表，参数不合法才抛 `ValueError`。
- 私有材料（`owner_user`/`owner_tenant` 非空）任何接口不可见；`can_show_raw=false` 只返回元数据与溯源，不返回正文。
- 同比/环比等派生值只由 `metrics.derived.derive` 算（CLAUDE.md §1.2），不在这里手写公式。
- 改契约必须同步改 `tests/contracts/dataapi_contract.py` 与契约文档。

## 怎么测
仓库根目录运行：
- `pytest tests/contracts/test_mock_dataapi.py`：免费、无数据库。
- `pytest tests/contracts/test_pg_dataapi.py`：`@pytest.mark.db`，`temp_db` 建随机空库、迁移后 `seed_sample`，再跑同一套契约。
- `pytest tests/dataapi`。

## 常见坑
- 实体、传导规则不是时点表，没有 `known_at`，其 `SourceCitation.known_at` 为 `None`；实体环节归属取 `core.entity.l3_node`。
- `PgDataApi` 没注入 `RetrievalService` 时只有 `search_blocks` 抛 `RuntimeError`，其余方法不受影响。
- 交易日历从行情推导：数据稀疏时某交易日若恰好所有实体都无行情，会被误判为休市。
- `sample.py` 的数字是示意数据，别拿去当真值引用。
