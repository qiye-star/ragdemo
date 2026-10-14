# db/migrations/ — 迁移脚本：只加不改，按文件名字典序执行

## 这个目录里有什么

33 个 `NNN_<说明>.sql`，号段 001–034。按主题粗分：001–006 扩展/枚举/实体与指标/事实/文档/观点与审计/`asof` 视图与角色（地基）；007–027 解析产物、去重、信源登记、摄取水位、回放、快照、告警等；030–034 文档表格网格、事实表引用、块元数据、解析逐页台账。

## 怎么用

```bash
set -a; . ./.env; set +a
uv run ragdemo db migrate      # 只执行尚未记录在 public.schema_migrations 的迁移
```

新增迁移：取下一个未用号（现在是 **035**），文件名 `035_<snake_or_中文说明>.sql`；写 `tests/db/test_migration_035.py`；`uv run ragdemo db migrate` 后跑 `ragdemo db check`。

## 硬规则

- **已执行过的迁移文件一个字节都不准改**：执行器记录 SHA-256 校验和（`public.schema_migrations`），内容变了抛 `MigrationChecksumMismatch`。纠错靠新增迁移（007_missing_time_order_checks 就是补 002/003 漏掉 CHECK 的先例）。
- 每条迁移是独立事务，失败整条回滚、之前已成功的保留；所以一条迁移里别混放无关改动。
- 文件头注释写清来源文档（`docs/..` §、`adr/..`）与背景；改动需同步那些文档。
- 时点事实表必须登记 `core.bitemporal_registry` 并建 `asof` 视图；非时点表（如 013 摄取水位）在注释里写明不登记的理由。
- 迁移里不得放密钥、真实口令、环境相关地址。
- ParadeDB 版本固定为 `0.25.9-pg18`（infra/docker-compose.yml）；bm25 索引建在迁移 004，升级镜像要重跑 P0 全部测试。

## 常见坑

- **号段并非连续**：有两个 `007`（`007_missing_time_order_checks.sql` 与 `007_parse_artifacts.sql`，version 以完整文件名 stem 为准，字典序先执行 missing_time_order），且 **没有 028、029**（027 之后直接 030）。不要“补齐”或重命名——会破坏校验和。
- `infra/backfill.md` 里写的「当前到 023」已过期，实际到 034。
- 并行建 bm25 索引曾触发 pg_search 崩溃，compose 已关并行；建测试库偶发整库重启先查这个。
