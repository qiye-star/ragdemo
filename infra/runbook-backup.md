# 备份与恢复 Runbook

自建 ParadeDB 没有托管 RDS 的自动备份、PITR 与跨可用区副本
（[adr/0002](../docs/adr/0002-self-hosted-paradedb-single-db.md) 的负面后果 1）。
这份手册是那笔成本的支付方式。

[adr/0009](../docs/adr/0009-chroma-as-vector-candidate-generator.md) 引入 Chroma 后，
需要备份的有状态服务从一个变成两个：PG（系统真相与时点权威）与 Chroma（向量候选生成器）。
**两者的快照必须对齐到同一时点**——如果恢复后 PG 与 Chroma 停在不同时刻，
就会出现"召回集与事实集不一致"：Chroma 里还能召回 PG 中已被撤回或更正的块，
或者反过来，PG 里已有的块在 Chroma 里还没出现。这正是 adr/0009 决策要结构性避免的错误，
备份/恢复环节如果对不齐时点，会把它从查询期引入到恢复期。

## 每日备份

`scripts/backup.sh` 在同一次运行里依次拍下 PG（`pg_dump`）与 Chroma（打包持久化目录）
两份快照，共用同一个 UTC 时间戳，这就是"对齐到同一时点"的落地方式。
生产环境如果对一致性窗口要求更严，应在拍摄前短暂暂停 Dagster 摄取。

cron（宿主机）：

```
0 3 * * * RAGDEMO_DSN=... RETENTION_DAYS=30 /opt/ragdemo/scripts/backup.sh >> /var/log/ragdemo-backup.log 2>&1
```

产物：

- PG：`ragdemo-<UTC 时间戳>.dump` 与同名 `.sha256`；
- Chroma：`ragdemo-chroma-<UTC 时间戳>.tar.gz` 与同名 `.sha256`，打包自容器
  `ragdemo-chroma` 的持久化目录 `/data`。

两者用相同的 `<UTC 时间戳>` 配对，保留 30 天。备份完成后同步到对象存储
（独立于数据库/Chroma 所在主机）。

## PITR

`postgresql.conf`：

```
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /wal-archive/%f && cp %p /wal-archive/%f'
```

WAL 归档目录与每日全量备份一同上传对象存储。恢复到任意时点时，
先 `pg_restore` 最近的全量备份，再重放归档 WAL 到目标时刻。

Chroma 没有 WAL/PITR 等价物，无法恢复到 PG 用 WAL 重放出的任意时点——
只能恢复到最近一次全量快照。因此 **PITR 场景下，Chroma 只能对齐到"晚于或等于"
目标时点的下一次全量快照**，恢复后必须立即跑 PG↔Chroma 一致性核对
（见下）确认偏移，而不是假设两者天然对齐。

## 恢复演练

**每月一次，不可跳过。** 没演练过的备份等于没有备份。

1. 起一个空的 ParadeDB 容器与空的 Chroma 容器（都与生产同版本 tag）；
2. `CONFIRM=yes scripts/restore.sh <最近的 PG dump>`——脚本会从文件名自动配对
   同一时间戳的 Chroma 快照，两者一起恢复；缺一个配对文件会直接失败，
   不会出现"只恢复了一半"的状态；
3. 核对脚本输出的三张表计数与备份当日的监控记录是否一致；
4. 跑 `ragdemo db check`（schema 不变量 + 时点泄漏自检）；
5. 跑 PG↔Chroma 一致性核对（P1c Task 10 的双跑一致性检查范畴，adr/0009 后果 2）：
   PG 中每个未被 supersede、有 embedding 的叶子块都应在 Chroma 中存在，反之亦然；
6. 跑 `pytest tests/test_p1_acceptance.py -m db`；
7. 把演练日期、耗时、发现的问题记入本文件末尾的演练日志。

## 演练日志

| 日期 | 耗时 | 恢复到 | 结果 | 备注 |
|---|---|---|---|---|
| | | | | |
