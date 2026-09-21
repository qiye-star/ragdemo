# 全部目标走 uv run，Windows 与 Linux 行为一致，不依赖预先激活虚拟环境。
COMPOSE := docker compose -f infra/docker-compose.yml

# 本地开发的缺省连接串，与 infra/docker-compose.yml 和 .env.example 保持一致。
# 端口是 5433 而不是 5432：开发机上 5432 常被既有 postgres 占用。
# 这里的口令只是本地回环容器的开发缺省值，不是任何环境的真实凭据；
# 真实凭据一律从环境变量注入，下面的 ?= 保证外部设置优先。
POSTGRES_PASSWORD ?= ragdemo
RAGDEMO_DB_PORT   ?= 5433
RAGDEMO_DSN       ?= postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:$(RAGDEMO_DB_PORT)/ragdemo
RAGDEMO_ADMIN_DSN ?= postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:$(RAGDEMO_DB_PORT)/postgres
export POSTGRES_PASSWORD RAGDEMO_DB_PORT RAGDEMO_DSN RAGDEMO_ADMIN_DSN

.PHONY: install up down lint typecheck test db-init seed test-schema accept-p0 clean

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

clean:
	$(COMPOSE) down -v
