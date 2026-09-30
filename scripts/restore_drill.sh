#!/usr/bin/env bash
# 备份恢复演练（阶段 4.6）：把现库备份恢复到一个**独立的临时容器**，不碰现库，
# 逐表核对行数与内容校验和，再跑 schema 不变量自检。演练结束销毁临时容器。
#
# 只依赖 docker（pg_dump / pg_restore / psql 都在 paradedb 容器里），不要求宿主机装 PostgreSQL 客户端。
#
# 用法：
#   scripts/restore_drill.sh            # 用现库当场拍一份备份 → 恢复到临时容器 → 核对
#   KEEP=1 scripts/restore_drill.sh     # 演练完保留临时容器，便于人工检查
#
# 环境变量：
#   SRC_CONTAINER  现库容器（默认 ragdemo-db）
#   SRC_DB / SRC_USER  现库名与用户（默认 ragdemo / postgres）
#   DRILL_PORT     临时容器映射到宿主机的端口（默认 55440）
set -euo pipefail

SRC_CONTAINER="${SRC_CONTAINER:-ragdemo-db}"
SRC_DB="${SRC_DB:-ragdemo}"
SRC_USER="${SRC_USER:-postgres}"
DRILL_PORT="${DRILL_PORT:-55440}"
DRILL_CONTAINER="ragdemo-restore-drill"
DRILL_PASSWORD="drill-$(date +%s)"
IMAGE="$(docker inspect --format '{{.Config.Image}}' "${SRC_CONTAINER}")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
START="$(date +%s)"

cleanup() {
  if [ "${KEEP:-}" != "1" ]; then
    docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT

# 校验和：每张表 "行数 + 全表文本的 md5"（行内容转文本后排序再聚合，与物理顺序无关）。
CHECKSUM_SQL="
DO \$\$
DECLARE r record; n bigint; h text;
BEGIN
  CREATE TEMP TABLE IF NOT EXISTS _sums (tbl text, n bigint, md5 text);
  TRUNCATE _sums;
  FOR r IN SELECT schemaname, tablename FROM pg_tables
            WHERE schemaname IN ('core','evals','audit','quality') ORDER BY 1, 2 LOOP
    EXECUTE format('SELECT count(*), coalesce(md5(string_agg(t::text, ''|'' ORDER BY t::text)), ''-'') FROM %I.%I t',
                   r.schemaname, r.tablename) INTO n, h;
    INSERT INTO _sums VALUES (r.schemaname || '.' || r.tablename, n, h);
  END LOOP;
END \$\$;
SELECT tbl || ' ' || n || ' ' || md5 FROM _sums ORDER BY tbl;
"

echo "[1/6] 备份现库 ${SRC_CONTAINER}/${SRC_DB}（pg_dump -Fc，快照一致）"
docker exec "${SRC_CONTAINER}" pg_dump -U "${SRC_USER}" -Fc -Z 9 -f "/tmp/drill-${STAMP}.dump" "${SRC_DB}"
docker cp "${SRC_CONTAINER}:/tmp/drill-${STAMP}.dump" "${WORK}/drill.dump"
docker exec "${SRC_CONTAINER}" rm -f "/tmp/drill-${STAMP}.dump"
echo "    备份大小：$(du -h "${WORK}/drill.dump" | cut -f1)"

echo "[2/6] 现库校验和（备份之后立即取；期间若有写入会被逐表报出来）"
docker exec "${SRC_CONTAINER}" psql -U "${SRC_USER}" -d "${SRC_DB}" -Atq -c "${CHECKSUM_SQL}" > "${WORK}/src.sums"

echo "[3/6] 起独立临时容器 ${DRILL_CONTAINER}（镜像 ${IMAGE}，端口 ${DRILL_PORT}）"
docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${DRILL_CONTAINER}" -e POSTGRES_PASSWORD="${DRILL_PASSWORD}" \
  -p "127.0.0.1:${DRILL_PORT}:5432" "${IMAGE}" >/dev/null
for _ in $(seq 1 60); do
  if docker exec "${DRILL_CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 2
done
docker exec "${DRILL_CONTAINER}" pg_isready -U postgres >/dev/null

echo "[4/6] 恢复到临时容器"
docker exec "${DRILL_CONTAINER}" createdb -U postgres "${SRC_DB}"
docker cp "${WORK}/drill.dump" "${DRILL_CONTAINER}:/tmp/drill.dump"
# 现库对象属于 app_owner 等角色：先在临时容器建同名角色，恢复时授权语句才不会失败。
for role in app_owner app_read app_write app_diag; do
  docker exec "${DRILL_CONTAINER}" psql -U postgres -d postgres -q -c "CREATE ROLE ${role}" 2>/dev/null || true
done
docker exec "${DRILL_CONTAINER}" pg_restore -U postgres --dbname "${SRC_DB}" --exit-on-error /tmp/drill.dump

echo "[5/6] 恢复库校验和并逐表比对"
docker exec "${DRILL_CONTAINER}" psql -U postgres -d "${SRC_DB}" -Atq -c "${CHECKSUM_SQL}" > "${WORK}/dst.sums"
TOTAL="$(wc -l < "${WORK}/src.sums" | tr -d ' ')"
if diff "${WORK}/src.sums" "${WORK}/dst.sums" > "${WORK}/diff.txt"; then
  echo "    ${TOTAL} 张表：行数与内容校验和全部一致"
  RESULT="一致"
else
  echo "    不一致的表：" >&2
  cat "${WORK}/diff.txt" >&2
  RESULT="不一致"
fi
ROWS="$(awk '{s+=$2} END {print s}' "${WORK}/dst.sums")"

echo "[6/6] 恢复库上跑 schema 不变量自检（ragdemo db check）"
if RAGDEMO_DSN="postgresql://postgres:${DRILL_PASSWORD}@127.0.0.1:${DRILL_PORT}/${SRC_DB}" \
     uv run ragdemo db check; then
  CHECK="通过"
else
  CHECK="失败"
fi

ELAPSED="$(( $(date +%s) - START ))"
echo
echo "演练结果：表 ${TOTAL} 张，共 ${ROWS} 行，校验和 ${RESULT}，db check ${CHECK}，耗时 ${ELAPSED}s"
echo "日志行：| $(date -u +%F) | ${ELAPSED}s | pg_dump 当场快照 | ${RESULT}；db check ${CHECK} | ${TOTAL} 张表 ${ROWS} 行，独立临时容器 |"
[ "${RESULT}" = "一致" ] && [ "${CHECK}" = "通过" ]
