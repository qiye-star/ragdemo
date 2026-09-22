# 全部目标走 uv run，Windows 与 Linux 行为一致，不依赖预先激活虚拟环境。
# docker compose 默认读 compose 文件所在目录的 .env（infra/.env），而仓库的
# .env 在根目录——不显式传 --env-file，egress-proxy 里新加的 TEXTIN_* 变量
# 会静默解析成空字符串（Phase 1B 已核实的阻断点之一）。
COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env

# 本地开发的缺省连接串，与 infra/docker-compose.yml 和 .env.example 保持一致。
# 端口是 5433 而不是 5432：开发机上 5432 常被既有 postgres 占用。
# 这里的口令只是本地回环容器的开发缺省值，不是任何环境的真实凭据；
# 真实凭据一律从环境变量注入，下面的 ?= 保证外部设置优先。
POSTGRES_PASSWORD ?= ragdemo
RAGDEMO_DB_PORT   ?= 5433
RAGDEMO_DSN       ?= postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:$(RAGDEMO_DB_PORT)/ragdemo
RAGDEMO_ADMIN_DSN ?= postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:$(RAGDEMO_DB_PORT)/postgres
export POSTGRES_PASSWORD RAGDEMO_DB_PORT RAGDEMO_DSN RAGDEMO_ADMIN_DSN

# 诊断接口（adr/0010）。只绑回环，端口避开 5433(PG)/8001(chroma)/
# 8080-8081(egress-proxy)/3000(dagster)。**不给 RAGDEMO_API_DSN 设默认
# 值**——默认值里必然带口令；缺它时 ragdemo serve 会显式报错指路，
# 不会替你猜一个。
RAGDEMO_API_HOST ?= 127.0.0.1
RAGDEMO_API_PORT ?= 8088
export RAGDEMO_API_HOST RAGDEMO_API_PORT

.PHONY: install up down lint typecheck test db-init seed test-schema accept-p0 accept-p1 clean dagster backup restore eval-retrieval serve seed-isolation-demo clear-isolation-demo

install:
	uv sync --all-packages

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

lint:
	uv run ruff check packages tests
	uv run ruff format --check packages tests

typecheck:
	uv run mypy

# 不含需要完整种子数据的验收断言，见 tests/test_p0_acceptance.py
test:
	uv run pytest -v -m "not seed_full"

db-init: up
	uv run ragdemo db migrate

seed:
	uv run ragdemo db seed

test-schema:
	uv run ragdemo db check
	uv run pytest tests/db -v -m "db and not seed_full"

# P0 阶段验收：docs/10-roadmap.md P0 表格七项，含尚未满足的种子计数项
accept-p0:
	uv run pytest tests/test_p0_acceptance.py -v -m db

# P1 阶段验收：docs/10-roadmap.md P1 表格九项。
# 注意其中三项（连续 5 天管线、间隔一周的双跑一致性、真实备份恢复演练）
# 只能做结构性验证，另有两项（recall@10 / recall@50）目前跑在夹具语料的
# 5 条用例上而非 06-retrieval.md §9.2 要求的 100 条生产用例——
# 跑绿不等于这五项已经真正验收，逐项说明见该文件头部的文档字符串。
accept-p1:
	uv run pytest tests/test_p1_acceptance.py -v -m db

# 检索评测 + 与主干基线比对，回退超容差（08 §4.1：0.02）以非零码退出，供 CI 门禁使用。
eval-retrieval:
	uv run ragdemo eval run --suite retrieval --gate

clean:
	$(COMPOSE) down -v

dagster:
	uv run dagster dev -m ragdemo.ingest.definitions

# 内网只读诊断接口（adr/0010）。需要 RAGDEMO_API_DSN（生产形态）或
# RAGDEMO_API_ALLOW_PRIVILEGED_DSN=1（开发逃生口，见 .env.example）之一。
serve:
	uv run ragdemo serve --host $(RAGDEMO_API_HOST) --port $(RAGDEMO_API_PORT)

# 权限隔离演示种子：公共对照 + 租户私有 + 用户 A/B 私有，供诊断界面的
# 隔离探针矩阵有数据可看。只在回环地址的数据库上生效。
seed-isolation-demo:
	uv run ragdemo db seed-isolation-demo --yes

clear-isolation-demo:
	uv run ragdemo db clear-isolation-demo --yes

# 备份 PG 与 Chroma 到同一时间戳的一对快照，见 infra/runbook-backup.md。
backup:
	bash scripts/backup.sh

# 恢复：FILE 指向 PG dump，脚本会自动配对同一时间戳的 Chroma 快照一起恢复。
# 例：make restore FILE=/var/backups/ragdemo/ragdemo-20260921T030000Z.dump
restore:
	CONFIRM=yes bash scripts/restore.sh $(FILE)
