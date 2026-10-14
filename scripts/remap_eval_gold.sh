#!/usr/bin/env bash
# 旧文档缓存重解析（ragdemo docs reparse-cached --yes）之后，生成评测 gold 块号重映射的报告与 SQL。
# 本脚本只读：不改任何评测数据。生成的 SQL 须人工审阅后再执行（见文末提示）。
# 说明与演练结果：docs/10.12-plan/验收记录/旧文档缓存重解析演练.md
set -euo pipefail

: "${RAGDEMO_DSN:?RAGDEMO_DSN 未设置}"
OUT_DIR="${1:-data/experiments/gold-remap-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "${OUT_DIR}"

uv run --no-sync ragdemo docs remap-gold \
  --report "${OUT_DIR}/gold-remap.tsv" \
  --sql-out "${OUT_DIR}/gold-remap.sql"

echo
echo "报告：${OUT_DIR}/gold-remap.tsv（kind = live / exact / overlap / lost；lost 需人工裁决）"
echo "SQL ：${OUT_DIR}/gold-remap.sql（一个事务；gold 与生成时不一致则整体报错回滚，可放心重复执行）"
echo "审阅后执行：psql \"\$RAGDEMO_DSN\" -v ON_ERROR_STOP=1 -f ${OUT_DIR}/gold-remap.sql"
echo "（或容器内：docker exec -i ragdemo-db psql -U postgres -d ragdemo -v ON_ERROR_STOP=1 < ${OUT_DIR}/gold-remap.sql）"
