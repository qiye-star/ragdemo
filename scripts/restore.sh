#!/usr/bin/env bash
# 从备份恢复。会覆盖目标库，因此要求显式确认。
#
# adr/0009 后果 1：PG 与 Chroma 必须恢复到同一时点，否则召回集（Chroma）与
# 事实集（PG）对不上。backup.sh 用同一个 STAMP 命名两份快照，这里反过来：
# 只接收 PG dump 路径，从文件名推出配对的 Chroma 快照，两者一起恢复。
# 如果配对的 Chroma 快照缺失，直接失败——绝不允许只恢复一半。
set -euo pipefail

FILE="${1:?用法: restore.sh <dump 文件>}"
: "${RAGDEMO_DSN:?RAGDEMO_DSN 未设置}"
CHROMA_CONTAINER="${CHROMA_CONTAINER:-ragdemo-chroma}"

if [ "${CONFIRM:-}" != "yes" ]; then
  echo "恢复会覆盖 ${RAGDEMO_DSN} 与 Chroma 容器 ${CHROMA_CONTAINER} 的现有数据。" >&2
  echo "确认后重跑: CONFIRM=yes $0 ${FILE}" >&2
  exit 1
fi

sha256sum --check "${FILE}.sha256"
pg_restore --clean --if-exists --no-owner --dbname "${RAGDEMO_DSN}" "${FILE}"

psql "${RAGDEMO_DSN}" -c "SELECT 'entity', count(*) FROM core.entity
                          UNION ALL SELECT 'doc_block', count(*) FROM core.doc_block
                          UNION ALL SELECT 'fin_fact', count(*) FROM core.fin_fact;"
echo "PG 恢复完成。请核对上表计数与备份时是否一致。"

# --- 配对的 Chroma 快照：文件名把 ragdemo-<STAMP>.dump 换成 ragdemo-chroma-<STAMP>.tar.gz ---
DUMP_DIR="$(dirname "${FILE}")"
DUMP_BASENAME="$(basename "${FILE}")"
STAMP="${DUMP_BASENAME#ragdemo-}"
STAMP="${STAMP%.dump}"
CHROMA_FILE="${DUMP_DIR}/ragdemo-chroma-${STAMP}.tar.gz"

if [ ! -f "${CHROMA_FILE}" ]; then
  echo "错误: 找不到配对的 Chroma 快照 ${CHROMA_FILE}。" >&2
  echo "只恢复 PG 会让召回集（Chroma）停在旧时点、与刚恢复的事实集不一致，拒绝继续。" >&2
  exit 1
fi
sha256sum --check "${CHROMA_FILE}.sha256"

docker exec "${CHROMA_CONTAINER}" sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null || true'
docker exec -i "${CHROMA_CONTAINER}" tar xzf - -C /data < "${CHROMA_FILE}"
docker restart "${CHROMA_CONTAINER}" >/dev/null
echo "Chroma 恢复完成: ${CHROMA_FILE}"

echo "恢复完成。接下来必须跑 PG↔Chroma 一致性核对（adr/0009 后果 2），确认偏移为 0。"
