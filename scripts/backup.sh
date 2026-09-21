#!/usr/bin/env bash
# 每日备份到对象存储。自建 ParadeDB 没有托管 RDS 的自动备份（adr/0002 的负面后果 1）。
#
# adr/0009 引入 Chroma 后，"单库"不再成立：PG（系统真相）与 Chroma（向量候选生成器）
# 是两个独立有状态服务，各自备份没有意义——恢复时如果只有一边对得上某个时点，
# 就会出现"召回集与事实集不一致"（Chroma 召回了 PG 已经撤回/更正的块，或反之）。
# 因此两个快照必须在同一次运行里、共用同一个 STAMP，尽量靠近同一时刻拍下。
# 生产环境应在拍摄窗口内短暂暂停写入（暂停 Dagster 摄取），把窗口降到零。
set -euo pipefail

RETENTION_DAYS="${RETENTION_DAYS:-30}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/ragdemo}"
CHROMA_CONTAINER="${CHROMA_CONTAINER:-ragdemo-chroma}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "${BACKUP_DIR}"

# --- PG：系统真相与时点权威（adr/0009 方案 B） ---
PG_TARGET="${BACKUP_DIR}/ragdemo-${STAMP}.dump"
pg_dump --format=custom --compress=9 --file="${PG_TARGET}" "${RAGDEMO_DSN:?RAGDEMO_DSN 未设置}"
sha256sum "${PG_TARGET}" > "${PG_TARGET}.sha256"
echo "PG 备份完成: ${PG_TARGET} ($(du -h "${PG_TARGET}" | cut -f1))"

# --- Chroma：向量候选生成器，只是候选排序信号，但也要能整体重建 ---
# Chroma 没有 pg_dump 式的逻辑导出，直接打包官方镜像的持久化目录（/data）。
# 用同一个 STAMP 命名，restore.sh 靠文件名把两份快照配成一对。
CHROMA_TARGET="${BACKUP_DIR}/ragdemo-chroma-${STAMP}.tar.gz"
if docker inspect "${CHROMA_CONTAINER}" >/dev/null 2>&1; then
  docker exec "${CHROMA_CONTAINER}" tar czf - -C /data . > "${CHROMA_TARGET}"
  sha256sum "${CHROMA_TARGET}" > "${CHROMA_TARGET}.sha256"
  echo "Chroma 备份完成: ${CHROMA_TARGET} ($(du -h "${CHROMA_TARGET}" | cut -f1))"
else
  echo "警告: 容器 ${CHROMA_CONTAINER} 不存在，跳过 Chroma 备份——PG 与 Chroma 快照将不再对齐" >&2
fi

# --- 保留策略：两类产物都按同样的天数清理 ---
find "${BACKUP_DIR}" -name 'ragdemo-*.dump*' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -name 'ragdemo-chroma-*.tar.gz*' -mtime "+${RETENTION_DAYS}" -delete
echo "已清理 ${RETENTION_DAYS} 天前的备份"
