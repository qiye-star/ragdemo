# Dagster 常驻进程镜像（阶段 4.1）：dagster-webserver 与 dagster-daemon 共用同一个镜像，
# 装的是仓库自己的两个包（ragdemo-core、ragdemo）及其锁定的依赖。
#
# 镜像里**没有任何供应商密钥**：Tushare / Wind / iFind / DeepSeek / 硅基流动 / TextIn 的凭据
# 都只在 egress-proxy 容器里，业务进程只知道 http://egress-proxy:<端口>（CLAUDE.md §3）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    DAGSTER_HOME=/dagster_home

RUN pip install --no-cache-dir uv

WORKDIR /app
# 先拷依赖清单再装依赖，代码改动不会让依赖层缓存失效。
COPY pyproject.toml uv.lock ./
COPY packages/ragdemo-core/pyproject.toml packages/ragdemo-core/pyproject.toml
COPY packages/ragdemo/pyproject.toml packages/ragdemo/pyproject.toml
COPY packages/ragdemo-core/src packages/ragdemo-core/src
COPY packages/ragdemo/src packages/ragdemo/src
RUN uv sync --frozen --all-packages --no-dev

COPY config ./config
COPY db ./db
COPY infra/dagster/dagster.yaml infra/dagster/workspace.yaml /dagster_home/
COPY infra/dagster/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
