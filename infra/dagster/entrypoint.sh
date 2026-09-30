#!/bin/sh
# 容器入口：把 .env 里写给宿主机用的回环地址（127.0.0.1）改成 compose 网络里的服务名，
# 然后 exec 真正的命令。这样同一份 .env 既能给宿主机上的 CLI 用，也能给容器里的 Dagster 用，
# 不用维护两份地址。只改"指向 egress-proxy 的地址"，数据库地址在 compose 里显式给出。
set -eu

for name in RAGDEMO_TUSHARE_MCP_URL RAGDEMO_TUSHARE_REST_URL RAGDEMO_WIND_MCP_URL \
            RAGDEMO_IFIND_MCP_BASE SILICONFLOW_BASE_URL MODEL_BASE_URL TEXTIN_BASE_URL; do
  value="$(printenv "$name" || true)"
  if [ -n "$value" ]; then
    export "$name=$(printf '%s' "$value" | sed 's#127\.0\.0\.1#egress-proxy#g; s#localhost#egress-proxy#g')"
  fi
done

exec "$@"
