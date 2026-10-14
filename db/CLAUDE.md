# db/ — 数据库迁移与种子数据（schema 的唯一来源）

## 这个目录里有什么

| 路径 | 用途 |
|---|---|
| `migrations/` | 编号 SQL 迁移，见 `migrations/CLAUDE.md` |
| `seed/` | 提交进仓库的种子 CSV（实体、别名、关系、环节、指标映射、传导规则）及 `seed/sources/`（整理底稿），见 `seed/CLAUDE.md` |

执行器与校验在 `packages/ragdemo-core/src/ragdemo_core/db/`（`migrate.py`、`invariants.py`），不在这里。

## 怎么用

```bash
make up                          # 起 ParadeDB（本机 5433）
set -a; . ./.env; set +a         # CLI 不读 .env，要先导出 RAGDEMO_DSN
uv run ragdemo db migrate        # 执行未执行的迁移（make db-init = up + migrate）
uv run ragdemo db seed           # 种子同步入库，可重复执行
uv run ragdemo db check          # schema 不变量 + 时点泄漏自检
uv run ragdemo db replay-check   # 时点泄漏回放（200 个历史 as_of 对哈希）
```

## 硬规则

- **不手工改表**（根 CLAUDE.md §3）。任何 schema/视图/角色/约束变更 = 新增一条迁移 + `tests/db/test_migration_NNN.py`。
- 时点事实表登记在 `core.bitemporal_registry`（迁移 005 建表），必须带 `valid_from/known_at/superseded_at`，对外只经 `asof` 视图（迁移 006）；新增时点表要同步登记、建视图、加 `superseded_at > known_at` CHECK。
- 应用/Agent 只查 `asof` schema 视图，不查基表（根 CLAUDE.md §1.1）；`ingested_at` 永不进 as_of 过滤。
- 改 schema 前先备份（`make backup`）；仓库里的惯例是 `data/export/ragdemo-pre-<迁移号>-<时间戳>.dump`。

## 常见坑

- 日常开发库是 `ragdemo`，测试用 `t_<hex>` 临时库，别混。
- 业务连接串 `RAGDEMO_DSN` 默认指向 5433，不是 5432。
