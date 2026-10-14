# data/ — 本地运行时数据（整个目录被 git 忽略，**不是仓库内容**）

根 `.gitignore` 有 `data/`，下面所有东西都不入库（只有本文件是 `git add -f` 强制跟踪的例外，改它后同样要 `-f`）。干净 clone 上这个目录不存在，要靠摄取/备份恢复重建。

## 这个目录里有什么

| 子目录 | 内容 | 能否重建 |
|---|---|---|
| `blob/` | 对象存储根目录（`RAGDEMO_BLOB_ROOT`）：`raw/`（原始公告 PDF 等）、`cninfo/`、`edgar/`（按公司/文件分目录）、`parse/textin/<content_hash>/`（TextIn 完整响应 JSON 与 Markdown 缓存） | 原始文件可重抓；**TextIn 缓存重建要重新按页付费**，视为贵重资产 |
| `export/` | 手工/脚本做的 PG 备份 dump（如 `ragdemo-pre-034-<时间戳>.dump`、`.sha256`），迁移与重解析前拍的回滚点 | 不可重建；删前确认有新备份 |
| `experiments/` | 脚本输出：`parse-compare-*.tsv`、`parse-census-*.tsv`、`det-md/`、`l0v2/`、`l1-pilot/`、holdout 日志等 | 可由 `scripts/` 重跑 |

Dagster 容器把本目录挂为 `/app/data`（`RAGDEMO_BLOB_ROOT=/app/data/blob`）。

## 怎么用

- 脚本默认把输出写到 `data/experiments/`；备份走 `make backup`（写 `BACKUP_DIR`，默认 `/var/backups/ragdemo`，不是这里），手工 dump 习惯放 `data/export/`。
- 恢复：`CONFIRM=yes bash scripts/restore.sh <dump>`；先用 `scripts/restore_drill.sh` 演练。

## 硬规则

- **不得提交**本目录任何文件；不要改 `.gitignore` 放开它。
- 内含原始文档与实体财务数据，**不得发往境外**（CLAUDE.md §0）；dump 里有全库内容，不要放进共享盘/聊天/PR。
- 不要删除 `blob/parse/` 的缓存来「释放空间」——`ragdemo docs reparse-cached` 依赖它做零成本重解析。
- 不手工改 blob 文件；blob key 与 `core.document.parse_json_ref` 等列一一对应，改了会断链。

## 常见坑

- 体积较大（约 1.1 GB blob、240 MB export、50 MB experiments），别在编辑器里全量索引。
- 仓库根还有一个未跟踪的 `data.zip`，它**不在** `data/` 里，所以 `.gitignore` 的 `data/` **不会**忽略它；提交前务必不要 `git add -A`，或把它移出仓库/加入忽略。
