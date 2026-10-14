# packages/ragdemo-core/src/ragdemo_core/db — 迁移执行器、时点会话、Schema 不变量

## 这个包做什么
把「时点一致」从文档约定变成可执行的东西：`migrate` 按校验和只增不改地执行 `db/migrations/*.sql`；
`as_of_session` 把 `as_of` 绑到事务（供 `asof.*` 视图与 RLS 读取）；`invariants` 在 CI 与
`ragdemo db check` 里检查双时态字段、视图、权限、反规范化列一致性，并跑时点泄漏自检。

## 关键模块
| 文件 | 职责 |
|---|---|
| `migrate.py` | `migrate(conn, dir)`：每条迁移独立事务、执行后立即 `commit`；已执行的文件内容变了抛 `MigrationChecksumMismatch`；版本记在 `public.schema_migrations` |
| `session.py` | `as_of_session(conn, as_of, tenant=, user=)`：设置 `app.as_of` / `app.tenant` / `app.user`（事务级）；naive datetime 抛 `NaiveDatetimeError` |
| `invariants.py` | `check_schema_invariants(conn)` 返回违规列表；`check_point_in_time_leaks(conn, probe_as_of)` 返回 `{检查名: 行数}`，空表示无泄漏 |

## 对外入口
- `ragdemo db migrate`、`ragdemo db check`（`packages/ragdemo/src/ragdemo/cli.py`）调用 `migrate` 与两个 `check_*`。
- `as_of_session` 被 `api/queries/*`、`dataapi/pg.py`、`retrieval/`、`embed/batch.py` 等 20+ 处使用，是读 `asof.*` 的唯一正路。
- 只依赖 `psycopg` 与标准库；**不得 import `ragdemo`**。

## 本包的硬规则
- 迁移只加不改：改已执行的 `db/migrations/NNN_*.sql` 会触发校验和失败，要纠正就新增一条。
- `migrate` 里的显式 `conn.commit()` 不能删：`conn.transaction()` 在外层是 SAVEPOINT，删掉后中途失败会把此前成功的迁移一并回滚（`migrate.py` docstring）。
- `invariants.py` 的 `_LEAK_QUERIES` 里 `known_at = ingested_at` 是**检测**用的，是 `tests/test_no_ingested_at_in_filters.py`
  唯一的白名单；其他任何地方不得把 `ingested_at` 放进比较/过滤（CLAUDE.md §1.1）。改这条查询要同步改白名单。
- 新增时点表必须登记进 `core.bitemporal_registry`，并带 `BITEMPORAL_COLUMNS`（7 列）、`superseded_at > known_at` 的 CHECK、
  对应 `asof.<表>` 视图，且 `app_read` 对基表无 SELECT；否则 `check_schema_invariants` 报违规。
- core/asof 对象不得由超级用户持有（超级用户绕过 RLS）。
- `doc_block` / `doc_table` 的反规范化列须与父行一致（`doc_table` 另与自身 `grid` JSON 一致）；改写入逻辑时这里会先红。

## 怎么测
仓库根目录运行，均需数据库（`@pytest.mark.db`，`temp_db` 夹具见上级 CLAUDE.md）：
`pytest tests/db`（`test_migrate.py`、`test_session.py`、`test_schema_invariants.py`、`test_point_in_time.py`、`test_asof_layer.py`、各 `test_migration_NNN.py`）。
`test_session.py::test_naive_datetime_is_rejected` 不需要数据库。

## 常见坑
- `check_point_in_time_leaks` 里 `sum()` 返回 `Decimal`，类型检查要同时接受 `int | Decimal`。
- 不变量 5 已按 `docs/10-roadmap.md` P0 验收收窄为「时点表对 `app_read` 无 SELECT」，与 `docs/02-data-model.md` §9 原文略有出入（代码注释已说明）。
- `doc_table` 检查前会探测表是否存在（迁移 030 之前的库没有）。
