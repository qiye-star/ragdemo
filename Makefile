# 全部目标走 uv run，Windows 与 Linux 行为一致，不依赖预先激活虚拟环境。
COMPOSE := docker compose -f infra/docker-compose.yml

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
