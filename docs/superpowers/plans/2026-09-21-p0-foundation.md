# P0 平台基座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成一个可复现的数据库基座——全量 schema 已建、时点防泄漏机制在数据库层面生效、种子数据可一键导入、不变量有自动化测试守着。

**Architecture:** 单库（自建 ParadeDB，含 `pg_search` + `pgvector`）+ 极简 SQL 迁移执行器（只加不改、带校验和）。应用层通过 `asof` schema 的安全视图读取，基表对读角色不授予 `SELECT`；时点由会话变量 `app.as_of` 承载，未设置时**报错**而非回退到 `now()`。

**Tech Stack:** Python 3.11+ / psycopg 3 / pytest / ruff / mypy / Docker Compose / ParadeDB (PostgreSQL 18)

**Spec:** [`docs/02-data-model.md`](../../02-data-model.md)、[`docs/03-point-in-time.md`](../../03-point-in-time.md)、[`docs/01-architecture.md`](../../01-architecture.md) §4–5
**工作流：** [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W0.1–W0.6
**阶段验收：** [`docs/10-roadmap.md`](../../10-roadmap.md) P0

## Global Constraints

以下约束对**每一个**任务都生效，不再逐条重复：

- Python **3.11+**；全部函数有完整类型注解；`ruff` 与 `mypy --strict` 必须通过。
- **TDD**：先写失败的测试，跑一遍确认它按预期失败，再写最小实现。
- **时点语义**：`known_at` 是「现实中最早可获知的时刻」，**不是入库时间**；入库时间叫 `ingested_at`，永远不得进入 `as_of` 过滤。
- **`as_of` 无默认值**：任何接受 `as_of` 的函数都不得提供默认值，且必须拒绝 naive datetime。
- **迁移只加不改**：已执行的迁移文件内容不可修改，纠错靠新增迁移。
- **密钥只在环境变量**：代码、日志、测试夹具、提交信息中一律不得出现真实密钥。
- ParadeDB 镜像**固定具体版本号**，不用 `latest`。
- 提交信息用 Conventional Commits（`feat:` / `fix:` / `test:` / `chore:`）。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 依赖、ruff、mypy、pytest 配置 |
| `Makefile` | `up` / `down` / `db-init` / `seed` / `test-schema` / `lint` / `typecheck` / `test` |
| `infra/docker-compose.yml` | ParadeDB 服务（固定版本、独立数据卷） |
| `.pre-commit-config.yaml` | ruff / mypy / detect-secrets |
| `.github/workflows/ci.yml` | lint → typecheck → test |
| `db/migrations/001_extensions_and_enums.sql` | 扩展、schema、枚举 |
| `db/migrations/002_entity_and_metric.sql` | 实体层 + 指标规则层 |
| `db/migrations/003_facts.sql` | 时点事实层 + `provider_snapshot` |
| `db/migrations/004_documents.sql` | 文档层 + BM25 / HNSW 索引 + `embedding_cache` |
| `db/migrations/005_opinions_evals_audit.sql` | 观点/事件 + 评测 + 审计 + `bitemporal_registry` |
| `db/migrations/006_asof_views_and_roles.sql` | `current_as_of()` + 七个 `asof` 视图 + 角色权限 + RLS |
| `src/ragdemo/db/migrate.py` | 迁移执行器 |
| `src/ragdemo/db/session.py` | `as_of_session` 上下文管理器 |
| `src/ragdemo/db/invariants.py` | Schema 不变量与时点泄漏自检查询 |
| `src/ragdemo/seed/loader.py` | 种子 CSV 导入（校验 + 写入） |
| `src/ragdemo/cli.py` | `ragdemo db migrate` / `ragdemo db seed` / `ragdemo db check` |
| `db/seed/*.csv` | 创始人录入的种子数据（W9.1 交付） |
| `tests/conftest.py` | 临时数据库 fixture |
| `tests/db/test_migrate.py` | 迁移执行器测试 |
| `tests/db/test_session.py` | `as_of_session` 测试 |
| `tests/db/test_schema_invariants.py` | 五条 schema 不变量 |
| `tests/db/test_point_in_time.py` | 时点语义与泄漏自检 |
| `tests/seed/test_loader.py` | 种子导入校验测试 |

---

## Task 1: 项目骨架与工具链

**Files:**
- Create: `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `Makefile`, `src/ragdemo/__init__.py`, `tests/__init__.py`
- Test: `tests/test_toolchain.py`

**Interfaces:**
- Consumes: 无
- Produces: 包名 `ragdemo`，导入路径 `src/ragdemo/`；Makefile 目标 `lint` / `typecheck` / `test`

- [ ] **Step 1: 写失败的测试**

`tests/test_toolchain.py`：

```python
"""工具链自检：包可导入，且关键约束已配置。"""
from __future__ import annotations

import tomllib
from pathlib import Path


def test_package_importable() -> None:
    import ragdemo

    assert ragdemo.__name__ == "ragdemo"


def test_python_floor_is_311() -> None:
    cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["project"]["requires-python"] == ">=3.11"


def test_mypy_is_strict() -> None:
    cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["tool"]["mypy"]["strict"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_toolchain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo'`

- [ ] **Step 3: 写最小实现**

`pyproject.toml`：

```toml
[project]
name = "ragdemo"
version = "0.1.0"
description = "AI 产业链时点研究引擎"
requires-python = ">=3.11"
dependencies = [
    "psycopg[binary]>=3.2",
    "click>=8.1",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.6", "mypy>=1.11", "pre-commit>=3.8", "detect-secrets>=1.5"]

[project.scripts]
ragdemo = "ragdemo.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ragdemo"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ANN", "RUF"]

[tool.mypy]
strict = true
python_version = "3.11"
files = ["src", "tests"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = ["db: 需要数据库的测试"]
```

`src/ragdemo/__init__.py`：

```python
"""AI 产业链时点研究引擎。"""
```

`tests/__init__.py`：空文件。

`.pre-commit-config.yaml`：

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.2
    hooks:
      - id: mypy
        additional_dependencies: [psycopg, click, pytest]
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
```

`Makefile`：

```makefile
.PHONY: lint typecheck test

lint:
	ruff check src tests
	ruff format --check src tests

typecheck:
	mypy

test:
	pytest -v
```

`.github/workflows/ci.yml`：

```yaml
name: ci
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: make lint
      - run: make typecheck
      - run: pytest -v -m "not db"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pip install -e ".[dev]" && pytest tests/test_toolchain.py -v && make lint && make typecheck`
Expected: 3 passed，lint 与 typecheck 均通过

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml Makefile .pre-commit-config.yaml .github src tests
git commit -m "chore: 初始化项目骨架与工具链"
```

---

## Task 2: 容器环境与数据库连通性

**Files:**
- Create: `infra/docker-compose.yml`, `.env.example`, `tests/conftest.py`, `tests/db/__init__.py`, `tests/db/test_connectivity.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Task 1 的 Makefile
- Produces: pytest fixture `temp_db() -> str`（返回一个全新空库的 DSN，测试结束自动删除）；环境变量 `RAGDEMO_ADMIN_DSN`

- [ ] **Step 1: 写失败的测试**

`tests/conftest.py`：

```python
"""测试夹具：为每个需要数据库的测试提供一个全新的临时库。"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

ADMIN_DSN = os.environ.get(
    "RAGDEMO_ADMIN_DSN", "postgresql://postgres:ragdemo@localhost:5432/postgres"
)


def _dsn_for(database: str) -> str:
    base, _, _ = ADMIN_DSN.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture()
def temp_db() -> Iterator[str]:
    """创建一个随机命名的空库，测试结束后强制删除。"""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _dsn_for(name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
```

`tests/db/__init__.py`：空文件。

`tests/db/test_connectivity.py`：

```python
"""容器与扩展的连通性自检。"""
from __future__ import annotations

import psycopg
import pytest

REQUIRED_EXTENSIONS = {"pg_search", "vector", "pgcrypto", "pg_trgm"}


@pytest.mark.db
def test_required_extensions_are_available(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM pg_available_extensions WHERE name = ANY(%s)",
            (sorted(REQUIRED_EXTENSIONS),),
        ).fetchall()
    assert {r[0] for r in rows} == REQUIRED_EXTENSIONS


@pytest.mark.db
def test_server_is_postgres_15_or_newer(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        (num,) = conn.execute("SHOW server_version_num").fetchone()  # type: ignore[misc]
    assert int(num) >= 150000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db -v -m db`
Expected: FAIL — `psycopg.OperationalError: connection failed`（容器还没起）

- [ ] **Step 3: 写最小实现**

`infra/docker-compose.yml`——**版本号固定，不用 `latest`**（[adr/0001](../../adr/0001-bm25-paradedb-pg-search.md) 的后果 2）：

```yaml
services:
  paradedb:
    # 版本固定。升级时必须重跑 P0 全部测试，见 docs/02-data-model.md §5.5
    image: paradedb/paradedb:0.25.9-pg18
    container_name: ragdemo-db
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-ragdemo}
      POSTGRES_DB: ragdemo
    ports:
      - "127.0.0.1:5432:5432"   # 只绑本地，不暴露公网
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d ragdemo"]
      interval: 3s
      timeout: 3s
      retries: 20

volumes:
  pgdata:
```

`.env.example`：

```
POSTGRES_PASSWORD=ragdemo
RAGDEMO_ADMIN_DSN=postgresql://postgres:ragdemo@localhost:5432/postgres
RAGDEMO_DSN=postgresql://postgres:ragdemo@localhost:5432/ragdemo
```

Makefile 追加：

```makefile
.PHONY: up down

up:
	docker compose -f infra/docker-compose.yml up -d --wait

down:
	docker compose -f infra/docker-compose.yml down
```

- [ ] **Step 4: 跑测试确认通过**

Run: `make up && pytest tests/db -v -m db`
Expected: 2 passed

> 若镜像 tag `0.25.9-pg18` 不存在，用 `docker run --rm paradedb/paradedb:latest psql --version` 确认当前实际版本后，把 compose 里的 tag 换成那个**具体版本号**，并在提交信息里写明。不要退回 `latest`。

- [ ] **Step 5: 提交**

```bash
git add infra .env.example Makefile tests/conftest.py tests/db
git commit -m "chore: 加入 ParadeDB 容器与连通性测试"
```

---

## Task 3: 迁移执行器

**Files:**
- Create: `src/ragdemo/db/__init__.py`, `src/ragdemo/db/migrate.py`, `tests/db/test_migrate.py`

**Interfaces:**
- Consumes: Task 2 的 `temp_db` fixture
- Produces:
  - `Migration(version: str, path: Path, sql: str)`，属性 `checksum: str`
  - `discover(directory: Path) -> list[Migration]`
  - `migrate(conn: psycopg.Connection, directory: Path) -> list[str]`（返回本次新执行的 version 列表）
  - `MigrationChecksumMismatch(RuntimeError)`

- [ ] **Step 1: 写失败的测试**

`tests/db/test_migrate.py`：

```python
"""迁移执行器：顺序执行、幂等、拒绝修改已执行的迁移。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import MigrationChecksumMismatch, discover, migrate


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


@pytest.mark.db
def test_applies_migrations_in_lexical_order(temp_db: str, tmp_path: Path) -> None:
    _write(tmp_path, "002_second.sql", "CREATE TABLE b (id int);")
    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id int);")

    with psycopg.connect(temp_db) as conn:
        applied = migrate(conn, tmp_path)

    assert applied == ["001_first", "002_second"]


@pytest.mark.db
def test_rerun_is_a_noop(temp_db: str, tmp_path: Path) -> None:
    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id int);")

    with psycopg.connect(temp_db) as conn:
        assert migrate(conn, tmp_path) == ["001_first"]
        assert migrate(conn, tmp_path) == []


@pytest.mark.db
def test_modifying_an_applied_migration_raises(temp_db: str, tmp_path: Path) -> None:
    """迁移只加不改（docs/11-sdlc.md §4.2）。改了要能被发现。"""
    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id int);")
    with psycopg.connect(temp_db) as conn:
        migrate(conn, tmp_path)

    _write(tmp_path, "001_first.sql", "CREATE TABLE a (id bigint);")
    with psycopg.connect(temp_db) as conn, pytest.raises(MigrationChecksumMismatch):
        migrate(conn, tmp_path)


@pytest.mark.db
def test_failed_migration_rolls_back_entirely(temp_db: str, tmp_path: Path) -> None:
    """一条迁移内部失败，它的前半部分也不得留下痕迹。"""
    _write(tmp_path, "001_bad.sql", "CREATE TABLE ok (id int); CREATE TABLE ok (id int);")

    with psycopg.connect(temp_db) as conn, pytest.raises(psycopg.errors.DuplicateTable):
        migrate(conn, tmp_path)

    with psycopg.connect(temp_db) as conn:
        (exists,) = conn.execute("SELECT to_regclass('public.ok') IS NOT NULL").fetchone()  # type: ignore[misc]
    assert exists is False


def test_discover_ignores_non_sql_files(tmp_path: Path) -> None:
    _write(tmp_path, "001_first.sql", "SELECT 1;")
    (tmp_path / "README.md").write_text("notes", encoding="utf-8")
    assert [m.version for m in discover(tmp_path)] == ["001_first"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.db'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/db/__init__.py`：

```python
"""数据库访问层：迁移、会话、不变量检查。"""
```

`src/ragdemo/db/migrate.py`：

```python
"""极简 SQL 迁移执行器。

设计取舍：不用 Alembic。本项目的 schema 是手写 DDL（含 ParadeDB 专有的
BM25 索引选项），ORM 迁移工具的自动生成能力用不上，反而增加一层抽象。
换来的是「迁移只加不改」可以用校验和强制（docs/11-sdlc.md §4.2）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    text PRIMARY KEY,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationChecksumMismatch(RuntimeError):
    """已执行的迁移文件内容被修改了。"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path) -> list[Migration]:
    """按文件名字典序返回目录下的全部 .sql 迁移。"""
    return [
        Migration(version=p.stem, path=p, sql=p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.sql"))
    ]


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.transaction():
        conn.execute(_MIGRATIONS_TABLE)
    rows = conn.execute("SELECT version, checksum FROM public.schema_migrations").fetchall()
    return {str(v): str(c) for v, c in rows}


def migrate(conn: psycopg.Connection, directory: Path) -> list[str]:
    """执行尚未执行的迁移，返回本次新执行的 version 列表。

    每条迁移在独立事务中执行：失败则整条回滚，不留半截 schema。
    """
    already = _applied(conn)
    newly: list[str] = []
    for m in discover(directory):
        if m.version in already:
            if already[m.version] != m.checksum:
                raise MigrationChecksumMismatch(
                    f"迁移 {m.version} 已执行但文件内容被修改。"
                    f"迁移只加不改，请新增一条迁移来纠正。"
                )
            continue
        with conn.transaction():
            conn.execute(m.sql)  # type: ignore[arg-type]
            conn.execute(
                "INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)",
                (m.version, m.checksum),
            )
        newly.append(m.version)
    return newly
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_migrate.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/db tests/db/test_migrate.py
git commit -m "feat: 加入 SQL 迁移执行器，校验和强制迁移只加不改"
```

---

## Task 4: 迁移 001 — 扩展、Schema、枚举

**Files:**
- Create: `db/migrations/001_extensions_and_enums.sql`, `tests/db/test_migration_001.py`

**Interfaces:**
- Consumes: Task 3 的 `migrate()`
- Produces: schema `core` / `asof` / `evals` / `audit` / `orchestration`；9 个 `core.*` 枚举类型

- [ ] **Step 1: 写失败的测试**

`tests/db/test_migration_001.py`：

```python
"""迁移 001：扩展、schema、枚举。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
EXPECTED_SCHEMAS = {"core", "asof", "evals", "audit", "orchestration"}
EXPECTED_ENUMS = {
    "entity_type", "entity_status", "relation_type", "metric_role",
    "block_type", "opinion_direction", "score_horizon", "score_track",
    "scorer", "confidence",
}


@pytest.mark.db
def test_schemas_and_enums_created(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)

        schemas = {
            r[0]
            for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                (sorted(EXPECTED_SCHEMAS),),
            ).fetchall()
        }
        enums = {
            r[0]
            for r in conn.execute(
                "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'core' AND t.typtype = 'e'"
            ).fetchall()
        }
        exts = {
            r[0]
            for r in conn.execute("SELECT extname FROM pg_extension").fetchall()
        }

    assert schemas == EXPECTED_SCHEMAS
    assert enums == EXPECTED_ENUMS
    assert {"vector", "pg_search", "pgcrypto", "pg_trgm"} <= exts
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_migration_001.py -v`
Expected: FAIL — `FileNotFoundError` 或断言失败（`db/migrations` 为空）

- [ ] **Step 3: 写最小实现**

`db/migrations/001_extensions_and_enums.sql`——内容**逐字复制**
[`docs/02-data-model.md`](../../02-data-model.md) §1.4 与 §1.5 的两段 SQL，文件开头加一行来源注释：

```sql
-- 来源：docs/02-data-model.md §1.4、§1.5。改动需同步该文档。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS asof;
CREATE SCHEMA IF NOT EXISTS evals;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS orchestration;

CREATE TYPE core.entity_type       AS ENUM ('listed','private','institution','government','index');
CREATE TYPE core.entity_status     AS ENUM ('active','suspended','delisted','merged','pre_ipo');
CREATE TYPE core.relation_type     AS ENUM ('supplies_to','customer_of','competes_with',
                                            'invests_in','depends_on','substitutes');
CREATE TYPE core.metric_role       AS ENUM ('leading','confirming');
CREATE TYPE core.block_type        AS ENUM ('paragraph','table','figure','title');
CREATE TYPE core.opinion_direction AS ENUM ('bull','bear','neutral');
CREATE TYPE core.score_horizon     AS ENUM ('1M','3M');
CREATE TYPE core.score_track       AS ENUM ('price','evidence');
CREATE TYPE core.scorer            AS ENUM ('auto','human');
CREATE TYPE core.confidence        AS ENUM ('high','medium','low','inferred');
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_migration_001.py -v`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add db/migrations/001_extensions_and_enums.sql tests/db/test_migration_001.py
git commit -m "feat(db): 迁移 001 扩展、schema 与枚举"
```

---

## Task 5: 迁移 002 — 实体层与指标规则层

**Files:**
- Create: `db/migrations/002_entity_and_metric.sql`, `tests/db/test_migration_002.py`

**Interfaces:**
- Consumes: 迁移 001 的枚举
- Produces: 表 `core.entity` / `entity_alias` / `entity_relation` / `entity_node_membership` / `node_metric` / `metric_source_map` / `ai_revenue_rule` / `propagation_rule`

- [ ] **Step 1: 写失败的测试**

`tests/db/test_migration_002.py`：

```python
"""迁移 002：实体层与指标规则层的关键约束。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    return conn


def _insert_entity(conn: psycopg.Connection, entity_id: str, primary: str, nodes: list[str]) -> None:
    conn.execute(
        "INSERT INTO core.entity "
        "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
        "VALUES (%s, %s, 'listed', '算力', 'AI芯片', %s, %s)",
        (entity_id, f"测试-{entity_id}", nodes, primary),
    )


@pytest.mark.db
def test_entity_id_format_is_enforced(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        _insert_entity(db, "688256", "云端训练芯片", ["云端训练芯片"])


@pytest.mark.db
def test_primary_node_must_be_in_l3_node(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        _insert_entity(db, "CN.688256", "先进封装", ["云端训练芯片"])


@pytest.mark.db
def test_relation_cannot_point_at_itself(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True):
        _insert_entity(db, "CN.688256", "云端训练芯片", ["云端训练芯片"])
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "INSERT INTO core.entity_relation "
                "(from_entity, to_entity, relation_type, valid_from, known_at, source, ingest_run_id) "
                "VALUES ('CN.688256','CN.688256','supplies_to','2024-01-01','2024-01-01','manual','r1')"
            )


@pytest.mark.db
def test_only_one_live_node_membership_per_pair(db: psycopg.Connection) -> None:
    """同一 (实体, 环节) 在任一时刻只能有一行有效——这是基准可复现的前提。"""
    with db.transaction(force_rollback=True):
        _insert_entity(db, "CN.688256", "云端训练芯片", ["云端训练芯片"])
        sql = (
            "INSERT INTO core.entity_node_membership "
            "(entity_id, l3_node, valid_from, known_at, source, ingest_run_id) "
            "VALUES ('CN.688256','云端训练芯片','2024-01-01','2024-01-01','manual','r1')"
        )
        db.execute(sql)
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(sql)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_migration_002.py -v`
Expected: FAIL — `UndefinedTable: relation "core.entity" does not exist`

- [ ] **Step 3: 写最小实现**

`db/migrations/002_entity_and_metric.sql`——逐字复制
[`docs/02-data-model.md`](../../02-data-model.md) **§2.1、§2.2、§2.3、§2.4、§3** 的全部 SQL 块
（含全部 `CREATE INDEX`），文件开头加：

```sql
-- 来源：docs/02-data-model.md §2、§3。改动需同步该文档。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_migration_002.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add db/migrations/002_entity_and_metric.sql tests/db/test_migration_002.py
git commit -m "feat(db): 迁移 002 实体层与指标规则层"
```

---

## Task 6: 迁移 003 — 时点事实层

**Files:**
- Create: `db/migrations/003_facts.sql`, `tests/db/test_migration_003.py`

**Interfaces:**
- Consumes: 迁移 002 的 `core.entity` / `core.node_metric`
- Produces: 表 `core.fin_fact` / `core.price_daily` / `core.provider_snapshot`；部分唯一索引 `fin_fact_live_uk` / `price_daily_live_uk`

- [ ] **Step 1: 写失败的测试**

`tests/db/test_migration_003.py`：

```python
"""迁移 003：时点事实层。重点验证「更正必须走 superseded_at」是数据库强制的。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

_FACT = (
    "INSERT INTO core.fin_fact "
    "(entity_id, metric_id, period, period_end, value, unit, valid_from, known_at, "
    " source, ingest_run_id) "
    "VALUES ('CN.688256','revenue_total','2024Q3','2024-09-30',%s,'CNY','2024-07-01',%s,"
    " 'tushare','r1')"
)


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity "
        "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片')"
    )
    conn.execute(
        "INSERT INTO core.node_metric "
        "(metric_id, metric_name, metric_role, frequency, source_type, definition, unit) "
        "VALUES ('revenue_total','营业收入','confirming','quarterly','filing','合并报表营业收入','CNY')"
    )
    conn.commit()
    return conn


@pytest.mark.db
def test_second_live_row_for_same_period_is_rejected(db: psycopg.Connection) -> None:
    """忘记给旧行打 superseded_at 就插新行 —— 唯一索引必须拦下来。"""
    with db.transaction(force_rollback=True):
        db.execute(_FACT, (12340.5, "2024-10-28 18:32+08"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(_FACT, (12500.0, "2025-01-15 19:00+08"))


@pytest.mark.db
def test_correction_flow_succeeds(db: psycopg.Connection) -> None:
    """按 docs/03-point-in-time.md §3 的流程走就应该成功，且两行共存。"""
    with db.transaction(force_rollback=True):
        db.execute(_FACT, (12340.5, "2024-10-28 18:32+08"))
        db.execute(
            "UPDATE core.fin_fact SET superseded_at = %s "
            "WHERE entity_id='CN.688256' AND metric_id='revenue_total' "
            "  AND period='2024Q3' AND superseded_at IS NULL",
            ("2025-01-15 19:00+08",),
        )
        db.execute(_FACT, (12500.0, "2025-01-15 19:00+08"))
        (n,) = db.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert n == 2


@pytest.mark.db
def test_superseded_at_must_be_after_known_at(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True):
        db.execute(_FACT, (12340.5, "2024-10-28 18:32+08"))
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "UPDATE core.fin_fact SET superseded_at = '2024-01-01 00:00+08' "
                "WHERE superseded_at IS NULL"
            )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_migration_003.py -v`
Expected: FAIL — `UndefinedTable: relation "core.fin_fact" does not exist`

- [ ] **Step 3: 写最小实现**

`db/migrations/003_facts.sql`——逐字复制
[`docs/02-data-model.md`](../../02-data-model.md) **§4.1、§4.2、§4.3** 的全部 SQL 块，开头加：

```sql
-- 来源：docs/02-data-model.md §4。改动需同步该文档。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_migration_003.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add db/migrations/003_facts.sql tests/db/test_migration_003.py
git commit -m "feat(db): 迁移 003 时点事实层，唯一索引强制更正流程"
```

---

## Task 7: 迁移 004 — 文档层与检索索引

这是 P0 最容易出错的一步：BM25 索引的选项语法与分词器行为都踩过坑
（[`docs/02-data-model.md`](../../02-data-model.md) §5.5）。因此单独成任务。

**Files:**
- Create: `db/migrations/004_documents.sql`, `tests/db/test_migration_004.py`

**Interfaces:**
- Consumes: 迁移 002 的 `core.entity`
- Produces: 表 `core.document` / `core.doc_block` / `core.embedding_cache`；索引 `doc_block_bm25`（BM25）与 `doc_block_embedding_hnsw`（HNSW）

- [ ] **Step 1: 写失败的测试**

`tests/db/test_migration_004.py`：

```python
"""迁移 004：文档层与两个检索索引。

重点验证 docs/02-data-model.md §5.5 记录的那个陷阱不会复发：
entity_id 必须用 keyword 分词器（大小写敏感），用 raw 会静默匹配 0 行。
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity "
        "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片')"
    )
    conn.execute(
        "INSERT INTO core.document "
        "(doc_id, entity_id, doc_type, title, publish_at, source, content_hash, "
        " version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES "
        "(1,'CN.688256','quarterly','三季报','2024-10-28 18:32+08','mock','h1',1,"
        " '2024-07-01','2024-10-28 18:32+08','r1')"
    )
    conn.execute(
        "INSERT INTO core.doc_block "
        "(doc_id, block_type, section_path, ordinal, page, content, is_leaf, "
        " entity_id, doc_type, publish_at, valid_from, known_at, source, ingest_run_id) "
        "VALUES (1,'paragraph','第三节 主营业务',1,12,"
        " '报告期内云端训练芯片出货量提升，智能计算收入同比增长 58.2%。',true,"
        " 'CN.688256','quarterly','2024-10-28 18:32+08','2024-07-01',"
        " '2024-10-28 18:32+08','mock','r1')"
    )
    conn.commit()
    return conn


@pytest.mark.db
def test_both_retrieval_indexes_exist(db: psycopg.Connection) -> None:
    defs = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='core'"
        ).fetchall()
    }
    assert "USING bm25" in defs["doc_block_bm25"]
    assert "USING hnsw" in defs["doc_block_embedding_hnsw"]


@pytest.mark.db
def test_chinese_bm25_search_works(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT block_id FROM core.doc_block WHERE content @@@ '云端训练芯片'"
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.db
def test_entity_id_term_is_case_sensitive(db: psycopg.Connection) -> None:
    """keyword 分词器：大写命中、小写不命中。用 raw 会正好相反，且不报错。"""
    (upper,) = db.execute(
        "SELECT count(*) FROM core.doc_block "
        "WHERE block_id @@@ paradedb.term('entity_id','CN.688256')"
    ).fetchone()  # type: ignore[misc]
    (lower,) = db.execute(
        "SELECT count(*) FROM core.doc_block "
        "WHERE block_id @@@ paradedb.term('entity_id','cn.688256')"
    ).fetchone()  # type: ignore[misc]
    assert (upper, lower) == (1, 0)


@pytest.mark.db
def test_embedding_cache_key_includes_model_and_owner(db: psycopg.Connection) -> None:
    """同内容不同模型必须能共存，否则换模型后会读到旧向量。"""
    vec = "[" + ",".join(["0"] * 1024) + "]"
    ins = (
        "INSERT INTO core.embedding_cache (content_hash, model, owner_user, embedding) "
        "VALUES ('h', %s, %s, %s)"
    )
    with db.transaction(force_rollback=True):
        db.execute(ins, ("bge-m3", "", vec))
        db.execute(ins, ("qwen3-embedding", "", vec))
        db.execute(ins, ("bge-m3", "u1", vec))
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(ins, ("bge-m3", "", vec))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_migration_004.py -v`
Expected: FAIL — `UndefinedTable: relation "core.document" does not exist`

- [ ] **Step 3: 写最小实现**

`db/migrations/004_documents.sql`——逐字复制
[`docs/02-data-model.md`](../../02-data-model.md) **§5.1、§5.2、§5.4、§5.5** 的 SQL 块，
以及 [`docs/05-document-pipeline.md`](../../05-document-pipeline.md) **§5.3** 的
`core.embedding_cache`。BM25 索引部分**照抄下面这段**（已经过实测修正，不要自己改分词器）：

```sql
-- 来源：docs/02-data-model.md §5.5。
-- entity_id / doc_type 必须用 keyword 而非 raw：raw 默认小写化 token，
-- 会让 paradedb.term('entity_id','CN.688256') 静默匹配 0 行。
-- 不要加 datetime_fields：pg_search v0.24.1 起该选项已废弃且完全无效。
CREATE INDEX doc_block_bm25 ON core.doc_block
USING bm25 (block_id, content, content_desc, section_path,
            entity_id, doc_type, is_leaf, known_at, superseded_at, publish_at)
WITH (
  key_field = 'block_id',
  text_fields = '{
    "content":      {"tokenizer": {"type": "chinese_lindera"}, "record": "position"},
    "content_desc": {"tokenizer": {"type": "chinese_lindera"}, "record": "position"},
    "section_path": {"tokenizer": {"type": "chinese_lindera"}, "record": "position"},
    "entity_id":    {"tokenizer": {"type": "keyword"}, "fast": true},
    "doc_type":     {"tokenizer": {"type": "keyword"}, "fast": true}
  }',
  boolean_fields = '{ "is_leaf": {"fast": true} }'
);
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_migration_004.py -v`
Expected: 4 passed

> `test_chinese_bm25_search_works` 若失败，说明该镜像版本没有 `chinese_lindera` 分词器。
> 换成 `chinese_compatible` 重跑，**并同步更新 `docs/02-data-model.md` §5.5 与本计划**——
> 文档与代码不一致是未完成状态（[`docs/11-sdlc.md`](../../11-sdlc.md) §2.4）。

- [ ] **Step 5: 提交**

```bash
git add db/migrations/004_documents.sql tests/db/test_migration_004.py
git commit -m "feat(db): 迁移 004 文档层与 BM25/HNSW 索引"
```

---

## Task 8: 迁移 005 — 观点、事件、评测与审计层

**Files:**
- Create: `db/migrations/005_opinions_evals_audit.sql`, `tests/db/test_migration_005.py`

**Interfaces:**
- Consumes: 迁移 002 / 004
- Produces: 表 `core.event` / `core.opinion` / `core.opinion_score` / `evals.*`（5 张）/ `audit.tool_call_log` / `core.bitemporal_registry` / `core.entity_resolution_queue`

- [ ] **Step 1: 写失败的测试**

`tests/db/test_migration_005.py`：

```python
"""迁移 005：观点、评测、审计。重点是「观点必须有依据」与「两轨不合并」。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

_OPINION = (
    "INSERT INTO core.opinion "
    "(entity_id, direction, confidence, thesis, evidence_blocks, evidence_facts, "
    " agent_name, prompt_version, model, run_id, as_of) "
    "VALUES ('CN.688256','bull','medium','测试论点',%s,%s,'fundamental','v1','m','r1',"
    " '2024-10-29 09:00+08') RETURNING opinion_id"
)


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity "
        "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片')"
    )
    conn.commit()
    return conn


@pytest.mark.db
def test_opinion_without_evidence_is_rejected(db: psycopg.Connection) -> None:
    """合规硬约束的最后一道防线（CLAUDE.md §0）。"""
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        db.execute(_OPINION, ([], []))


@pytest.mark.db
def test_opinion_with_evidence_is_accepted(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True):
        row = db.execute(_OPINION, ([1, 2], [])).fetchone()
    assert row is not None


@pytest.mark.db
def test_price_and_evidence_tracks_coexist(db: psycopg.Connection) -> None:
    """adr/0007：两轨分开存，UNIQUE 是 (opinion_id, track, horizon)。"""
    with db.transaction(force_rollback=True):
        (oid,) = db.execute(_OPINION, ([1], [])).fetchone()  # type: ignore[misc]
        ins = (
            "INSERT INTO core.opinion_score "
            "(opinion_id, track, horizon, scored_at, score, outcome_desc, scorer) "
            "VALUES (%s, %s, '1M', now(), 1, '测试', %s)"
        )
        db.execute(ins, (oid, "price", "auto"))
        db.execute(ins, (oid, "evidence", "human"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(ins, (oid, "price", "auto"))


@pytest.mark.db
def test_bitemporal_registry_lists_seven_tables(db: psycopg.Connection) -> None:
    rows = db.execute("SELECT table_name::text FROM core.bitemporal_registry").fetchall()
    assert {r[0] for r in rows} == {
        "core.fin_fact", "core.price_daily", "core.document", "core.doc_block",
        "core.entity_relation", "core.entity_node_membership", "core.event",
    }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_migration_005.py -v`
Expected: FAIL — `UndefinedTable: relation "core.opinion" does not exist`

- [ ] **Step 3: 写最小实现**

`db/migrations/005_opinions_evals_audit.sql`——逐字复制
[`docs/02-data-model.md`](../../02-data-model.md) **§6、§7、§8、§9** 的全部 SQL 块，
以及 [`docs/04-ingestion.md`](../../04-ingestion.md) **§4** 的 `core.entity_resolution_queue`。
开头加：

```sql
-- 来源：docs/02-data-model.md §6–§9、docs/04-ingestion.md §4。改动需同步这两份文档。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_migration_005.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add db/migrations/005_opinions_evals_audit.sql tests/db/test_migration_005.py
git commit -m "feat(db): 迁移 005 观点、评测与审计层"
```

---

## Task 9: 迁移 006 — 时点安全层（视图、角色、RLS）

这是 P0 最重要的一步。它把「所有读取都带 `as_of`」从**约定**变成**数据库层面做不到违反**。

**Files:**
- Create: `db/migrations/006_asof_views_and_roles.sql`, `tests/db/test_asof_layer.py`

**Interfaces:**
- Consumes: 迁移 002–005 的七张时点表
- Produces: 函数 `asof.current_as_of() -> timestamptz`；视图 `asof.fin_fact` / `price_daily` / `document` / `doc_block` / `entity_relation` / `entity_node_membership` / `event`；角色 `app_read` / `app_write`；`core.document` 与 `core.doc_block` 上的 RLS 策略

- [ ] **Step 1: 写失败的测试**

`tests/db/test_asof_layer.py`：

```python
"""时点安全层：视图 + 角色 + RLS。

这组测试是 P0 的核心验收项（docs/10-roadmap.md P0 最后两行）。
它们证明时点防泄漏是物理生效的，而不只是写在文档里。
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
BITEMPORAL_VIEWS = {
    "fin_fact", "price_daily", "document", "doc_block",
    "entity_relation", "entity_node_membership", "event",
}


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute("CREATE USER apptest PASSWORD 'notasecret'")
    conn.execute("GRANT app_read TO apptest")
    conn.commit()
    return conn


@pytest.mark.db
def test_every_bitemporal_table_has_an_asof_view(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT table_name FROM information_schema.views WHERE table_schema='asof'"
    ).fetchall()
    assert {r[0] for r in rows} == BITEMPORAL_VIEWS


@pytest.mark.db
def test_reading_without_as_of_raises_not_returns_empty(db: psycopg.Connection) -> None:
    """未设时点必须报错。返回空集是最危险的行为——它让回测静默得出错误结论。"""
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.InvalidParameterValue):
        db.execute("SELECT count(*) FROM asof.fin_fact").fetchone()


@pytest.mark.db
def test_app_read_cannot_touch_base_tables(temp_db: str, db: psycopg.Connection) -> None:
    """core 对读角色不授予 SELECT：绕过 asof 视图在数据库层面就做不到。"""
    assert db is not None  # 确保 fixture 已建好角色
    with psycopg.connect(temp_db, user="apptest", password="notasecret") as c:  # noqa: S106
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM core.fin_fact")


@pytest.mark.db
def test_app_write_can_only_update_superseded_at(db: psycopg.Connection) -> None:
    """列级权限：能打失效标记，改不了任何值字段。"""
    (whole_table,) = db.execute(
        "SELECT has_table_privilege('app_write','core.fin_fact','UPDATE')"
    ).fetchone()  # type: ignore[misc]
    (superseded,) = db.execute(
        "SELECT has_column_privilege('app_write','core.fin_fact','superseded_at','UPDATE')"
    ).fetchone()  # type: ignore[misc]
    (value,) = db.execute(
        "SELECT has_column_privilege('app_write','core.fin_fact','value','UPDATE')"
    ).fetchone()  # type: ignore[misc]
    assert (whole_table, superseded, value) == (False, True, False)


@pytest.mark.db
def test_rls_enabled_on_document_tables(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT relname, relrowsecurity FROM pg_class "
        "WHERE relnamespace='core'::regnamespace AND relname IN ('document','doc_block')"
    ).fetchall()
    assert dict(rows) == {"document": True, "doc_block": True}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_asof_layer.py -v`
Expected: FAIL — `UndefinedObject: schema "asof" has no views` / `role "app_read" does not exist`

- [ ] **Step 3: 写最小实现**

`db/migrations/006_asof_views_and_roles.sql`——逐字复制
[`docs/03-point-in-time.md`](../../03-point-in-time.md) **§4.1 与 §4.2** 的全部 SQL
（`current_as_of()` 函数 + 七个视图 + 角色与授权），以及
[`docs/09-compliance-security.md`](../../09-compliance-security.md) **§3.2** 的 RLS 策略。

角色创建要幂等（迁移可能在已有角色的库上跑），在 §4.2 的 `CREATE ROLE` 前加：

```sql
-- 来源：docs/03-point-in-time.md §4、docs/09-compliance-security.md §3.2。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_read') THEN
    CREATE ROLE app_read;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_write') THEN
    CREATE ROLE app_write;
  END IF;
END $$;
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_asof_layer.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add db/migrations/006_asof_views_and_roles.sql tests/db/test_asof_layer.py
git commit -m "feat(db): 迁移 006 时点安全层，未设 as_of 读取直接报错"
```

---

## Task 10: `as_of_session` 上下文管理器

**Files:**
- Create: `src/ragdemo/db/session.py`, `tests/db/test_session.py`

**Interfaces:**
- Consumes: 迁移 006 的 `asof.current_as_of()`
- Produces:
  - `NaiveDatetimeError(ValueError)`
  - `as_of_session(conn, as_of, *, tenant=None, user=None) -> Iterator[psycopg.Connection]`（上下文管理器）

- [ ] **Step 1: 写失败的测试**

`tests/db/test_session.py`：

```python
"""as_of_session：把时点绑定在事务上，退出即失效。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate
from ragdemo.db.session import NaiveDatetimeError, as_of_session

MIGRATIONS = Path("db/migrations")
AS_OF = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.commit()
    return conn


def test_naive_datetime_is_rejected() -> None:
    """naive datetime 的行为随服务器时区变化，必须在入口拒绝。"""
    with pytest.raises(NaiveDatetimeError):
        with as_of_session(None, datetime(2024, 12, 31)):  # type: ignore[arg-type]
            pass


@pytest.mark.db
def test_as_of_is_visible_inside_session(db: psycopg.Connection) -> None:
    with as_of_session(db, AS_OF) as conn:
        (value,) = conn.execute("SELECT asof.current_as_of()").fetchone()  # type: ignore[misc]
    assert value == AS_OF


@pytest.mark.db
def test_as_of_does_not_leak_after_session(db: psycopg.Connection) -> None:
    """连接池复用时最容易出的 bug：上一个请求的 as_of 泄漏给下一个。"""
    with as_of_session(db, AS_OF):
        pass
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        db.execute("SELECT asof.current_as_of()").fetchone()


@pytest.mark.db
def test_tenant_and_user_are_set(db: psycopg.Connection) -> None:
    with as_of_session(db, AS_OF, tenant="t1", user="u1") as conn:
        row = conn.execute(
            "SELECT current_setting('app.tenant'), current_setting('app.user')"
        ).fetchone()
    assert row == ("t1", "u1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.db.session'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/db/session.py`：

```python
"""时点会话：把 as_of 绑定到一个事务上。

用 SET LOCAL 语义（set_config 的第三个参数为 true）而不是 SET：
时点随事务结束自动清除，连接池复用时不会把上一个请求的 as_of 泄漏给下一个。
SET LOCAL 本身不支持参数占位符，所以走 set_config()。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg


class NaiveDatetimeError(ValueError):
    """as_of 必须带时区。"""


@contextmanager
def as_of_session(
    conn: psycopg.Connection,
    as_of: datetime,
    *,
    tenant: str | None = None,
    user: str | None = None,
) -> Iterator[psycopg.Connection]:
    """开启一个锁定时点的事务。

    Args:
        conn: 数据库连接。
        as_of: 查询假设的时刻，**必须带时区**，无默认值。
        tenant: 租户标识，供 RLS 使用；None 表示只看公共数据。
        user: 用户标识，供 RLS 使用；None 表示只看公共数据。

    Raises:
        NaiveDatetimeError: as_of 不带时区。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise NaiveDatetimeError(f"as_of 必须带时区，收到 naive datetime: {as_of!r}")

    with conn.transaction():
        conn.execute("SELECT set_config('app.as_of', %s, true)", (as_of.isoformat(),))
        conn.execute("SELECT set_config('app.tenant', %s, true)", (tenant or "",))
        conn.execute("SELECT set_config('app.user', %s, true)", (user or "",))
        yield conn
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_session.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/db/session.py tests/db/test_session.py
git commit -m "feat(db): 加入 as_of_session，时点绑定事务且不跨请求泄漏"
```

---

## Task 11: Schema 不变量与时点泄漏自检

**Files:**
- Create: `src/ragdemo/db/invariants.py`, `tests/db/test_schema_invariants.py`, `tests/db/test_point_in_time.py`

**Interfaces:**
- Consumes: 迁移 001–006、`as_of_session`
- Produces:
  - `BITEMPORAL_COLUMNS: frozenset[str]`
  - `check_schema_invariants(conn) -> list[str]`（返回违规描述列表，空列表 = 通过）
  - `check_point_in_time_leaks(conn, probe_as_of) -> dict[str, int]`（每条自检查询的违规行数）

- [ ] **Step 1: 写失败的测试**

`tests/db/test_schema_invariants.py`：

```python
"""docs/02-data-model.md §9 的五条不变量。任何一条失败即 CI 红灯。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.invariants import check_schema_invariants
from ragdemo.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.mark.db
def test_all_schema_invariants_hold(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        violations = check_schema_invariants(conn)
    assert violations == []


@pytest.mark.db
def test_invariant_detects_a_missing_column(temp_db: str) -> None:
    """检查器本身要能发现问题，否则它只是个永远返回空列表的摆设。"""
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute(
            "CREATE TABLE core.broken (id int, known_at timestamptz NOT NULL)"
        )
        conn.execute(
            "INSERT INTO core.bitemporal_registry (table_name) VALUES ('core.broken')"
        )
        violations = check_schema_invariants(conn)
    assert any("core.broken" in v for v in violations)
```

`tests/db/test_point_in_time.py`：

```python
"""docs/03-point-in-time.md §5 的五条泄漏自检 + 视图的时点过滤行为。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.db.invariants import check_point_in_time_leaks
from ragdemo.db.migrate import migrate
from ragdemo.db.session import as_of_session

MIGRATIONS = Path("db/migrations")
PROBE = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity "
        "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片')"
    )
    conn.execute(
        "INSERT INTO core.node_metric "
        "(metric_id, metric_name, metric_role, frequency, source_type, definition, unit) "
        "VALUES ('revenue_total','营业收入','confirming','quarterly','filing','合并口径','CNY')"
    )
    conn.commit()
    return conn


@pytest.mark.db
def test_clean_database_has_no_leaks(db: psycopg.Connection) -> None:
    assert check_point_in_time_leaks(db, PROBE) == {}


@pytest.mark.db
def test_leak_check_detects_known_at_before_period_end(db: psycopg.Connection) -> None:
    """known_at 早于期末 = 提前知道了还没发生的事。"""
    db.execute(
        "INSERT INTO core.fin_fact "
        "(entity_id, metric_id, period, period_end, value, unit, valid_from, known_at,"
        " source, ingest_run_id) "
        "VALUES ('CN.688256','revenue_total','2024Q3','2024-09-30',1,'CNY','2024-07-01',"
        " '2024-08-01 00:00+08','tushare','r1')"
    )
    db.commit()
    assert check_point_in_time_leaks(db, PROBE)["known_at_before_period_end"] == 1


@pytest.mark.db
def test_asof_view_hides_future_rows(db: psycopg.Connection) -> None:
    """视图的核心行为：as_of 之后才可知的数据必须看不见。"""
    db.execute(
        "INSERT INTO core.fin_fact "
        "(entity_id, metric_id, period, period_end, value, unit, valid_from, known_at,"
        " source, ingest_run_id) "
        "VALUES ('CN.688256','revenue_total','2024Q3','2024-09-30',1,'CNY','2024-07-01',"
        " '2024-10-28 18:32+08','tushare','r1')"
    )
    db.commit()

    with as_of_session(db, datetime(2024, 10, 1, tzinfo=UTC)) as conn:
        (before,) = conn.execute("SELECT count(*) FROM asof.fin_fact").fetchone()  # type: ignore[misc]
    with as_of_session(db, datetime(2024, 11, 1, tzinfo=UTC)) as conn:
        (after,) = conn.execute("SELECT count(*) FROM asof.fin_fact").fetchone()  # type: ignore[misc]

    assert (before, after) == (0, 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/db/test_schema_invariants.py tests/db/test_point_in_time.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.db.invariants'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/db/invariants.py`：

```python
"""Schema 不变量与时点泄漏自检。

这些检查比文档更能防止时点语义被悄悄破坏：文档会被忽略，CI 红灯不会。
不变量来自 docs/02-data-model.md §9，泄漏自检来自 docs/03-point-in-time.md §5。
"""
from __future__ import annotations

from datetime import datetime

import psycopg

BITEMPORAL_COLUMNS = frozenset(
    {
        "valid_from", "known_at", "superseded_at",
        "source", "source_ref", "ingest_run_id", "ingested_at",
    }
)

_LEAK_QUERIES: dict[str, str] = {
    "known_at_before_period_end": """
        SELECT count(*) FROM core.fin_fact WHERE known_at::date < period_end
    """,
    "known_at_equals_ingested_at_on_backfill": """
        SELECT count(*) FROM core.fin_fact
         WHERE known_at = ingested_at AND ingested_at::date - valid_from > 400
    """,
    "superseded_before_known": """
        SELECT (SELECT count(*) FROM core.fin_fact
                 WHERE superseded_at IS NOT NULL AND superseded_at <= known_at)
             + (SELECT count(*) FROM core.doc_block
                 WHERE superseded_at IS NOT NULL AND superseded_at <= known_at)
    """,
    "multiple_live_rows_at_probe": """
        SELECT coalesce(sum(n) - count(*), 0) FROM (
            SELECT count(*) AS n FROM core.fin_fact
             WHERE known_at <= %(probe)s
               AND (superseded_at IS NULL OR superseded_at > %(probe)s)
             GROUP BY entity_id, metric_id, period HAVING count(*) > 1
        ) t
    """,
    "opinion_cites_future_block": """
        SELECT count(*) FROM core.opinion o
          JOIN core.doc_block b ON b.block_id = ANY (o.evidence_blocks)
         WHERE b.known_at > o.as_of
    """,
}


def check_schema_invariants(conn: psycopg.Connection) -> list[str]:
    """返回违规描述列表；空列表表示全部通过。"""
    violations: list[str] = []
    registered = [
        str(r[0])
        for r in conn.execute("SELECT table_name::text FROM core.bitemporal_registry").fetchall()
    ]

    for qualified in registered:
        schema, _, table = qualified.partition(".")

        cols = {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            ).fetchall()
        }
        missing = BITEMPORAL_COLUMNS - cols
        if missing:
            violations.append(f"{qualified} 缺少时点公共字段: {sorted(missing)}")

        (has_check,) = conn.execute(
            "SELECT count(*) > 0 FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = 'c' "
            "  AND pg_get_constraintdef(oid) ILIKE %s",
            (qualified, "%superseded_at%known_at%"),
        ).fetchone()  # type: ignore[misc]
        if not has_check:
            violations.append(f"{qualified} 缺少 superseded_at > known_at 的 CHECK 约束")

        (has_view,) = conn.execute(
            "SELECT count(*) > 0 FROM information_schema.views "
            "WHERE table_schema = 'asof' AND table_name = %s",
            (table,),
        ).fetchone()  # type: ignore[misc]
        if not has_view:
            violations.append(f"{qualified} 没有对应的 asof.{table} 视图")

    (block_mismatch,) = conn.execute(
        "SELECT count(*) FROM ("
        "  SELECT b.block_id FROM core.doc_block b JOIN core.document d USING (doc_id)"
        "   WHERE b.entity_id IS DISTINCT FROM d.entity_id"
        "      OR b.doc_type  IS DISTINCT FROM d.doc_type"
        "      OR b.known_at  IS DISTINCT FROM d.known_at"
        "   LIMIT 1000) t"
    ).fetchone()  # type: ignore[misc]
    if block_mismatch:
        violations.append(f"doc_block 的反规范化列与 document 不一致: {block_mismatch} 行")

    (core_readable,) = conn.execute(
        "SELECT count(*) > 0 FROM information_schema.role_table_grants "
        "WHERE grantee = 'app_read' AND table_schema = 'core' AND privilege_type = 'SELECT'"
    ).fetchone()  # type: ignore[misc]
    if core_readable:
        violations.append("app_read 不应对 core schema 有 SELECT 权限")

    return violations


def check_point_in_time_leaks(
    conn: psycopg.Connection, probe_as_of: datetime
) -> dict[str, int]:
    """跑 docs/03-point-in-time.md §5 的自检查询，返回有违规的项与行数。

    返回空字典表示没有泄漏。
    """
    found: dict[str, int] = {}
    for name, sql in _LEAK_QUERIES.items():
        (n,) = conn.execute(sql, {"probe": probe_as_of}).fetchone()  # type: ignore[misc]
        if n:
            found[name] = int(n)
    return found
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/db/test_schema_invariants.py tests/db/test_point_in_time.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/db/invariants.py tests/db/test_schema_invariants.py tests/db/test_point_in_time.py
git commit -m "feat(db): 加入 schema 不变量与时点泄漏自检"
```

---

## Task 12: 种子 CSV 导入工具

**Files:**
- Create: `src/ragdemo/seed/__init__.py`, `src/ragdemo/seed/loader.py`, `tests/seed/__init__.py`, `tests/seed/test_loader.py`, `db/seed/README.md`
- Test: `tests/seed/test_loader.py`

**Interfaces:**
- Consumes: 迁移 002
- Produces:
  - `SeedError(ValueError)`
  - `load_entities(conn, csv_path) -> int`（同时写 `core.entity` 与 `core.entity_node_membership`）
  - `load_aliases(conn, csv_path) -> int`
  - `load_relations(conn, csv_path) -> int`
  - `load_metrics(conn, csv_path) -> int`
  - `load_propagation_rules(conn, csv_path) -> int`
  - `load_all(conn, seed_dir) -> dict[str, int]`

- [ ] **Step 1: 写失败的测试**

`tests/seed/test_loader.py`：

```python
"""种子导入：校验优先于写入，坏数据不能进库。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.db.migrate import migrate
from ragdemo.seed.loader import SeedError, load_all, load_entities

MIGRATIONS = Path("db/migrations")

HEADER = (
    "entity_id,name_full,name_short,name_en,entity_type,market,tushare_code,"
    "l1_layer,l2_segment,l3_node,primary_node,hq_country,status,listed_date,"
    "currency,fiscal_year_end,notes\n"
)
GOOD_ROW = (
    "CN.688256,寒武纪-U,寒武纪,Cambricon,listed,SSE,688256.SH,"
    "算力,AI芯片,云端训练芯片|边缘推理芯片,云端训练芯片,CN,active,2020-07-20,"
    "CNY,12,\n"
)


@pytest.fixture()
def db(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.commit()
    return conn


def _csv(tmp_path: Path, rows: str, name: str = "entity.csv") -> Path:
    p = tmp_path / name
    p.write_text(HEADER + rows, encoding="utf-8")
    return p


@pytest.mark.db
def test_loads_entity_and_node_membership(db: psycopg.Connection, tmp_path: Path) -> None:
    """实体的环节归属必须同时写进时点表，否则回测基准不可复现。"""
    n = load_entities(db, _csv(tmp_path, GOOD_ROW))
    (entities,) = db.execute("SELECT count(*) FROM core.entity").fetchone()  # type: ignore[misc]
    (memberships,) = db.execute(
        "SELECT count(*) FROM core.entity_node_membership"
    ).fetchone()  # type: ignore[misc]
    assert (n, entities, memberships) == (1, 1, 2)


@pytest.mark.db
def test_membership_known_at_comes_from_listed_date(
    db: psycopg.Connection, tmp_path: Path
) -> None:
    """手工数据的 known_at 取 valid_from 当日 00:00（docs/03-point-in-time.md §6.3）。

    绝不能取 now()——那样这条归属在任何历史回测中都不可见。
    """
    load_entities(db, _csv(tmp_path, GOOD_ROW))
    row = db.execute(
        "SELECT valid_from, known_at FROM core.entity_node_membership LIMIT 1"
    ).fetchone()
    assert row is not None
    valid_from, known_at = row
    assert known_at.date() == valid_from
    assert (known_at.hour, known_at.minute) == (0, 0)


@pytest.mark.db
def test_primary_node_not_in_l3_is_rejected_before_any_write(
    db: psycopg.Connection, tmp_path: Path
) -> None:
    bad = GOOD_ROW.replace(",云端训练芯片,CN,", ",先进封装,CN,")
    with pytest.raises(SeedError, match="primary_node"):
        load_entities(db, _csv(tmp_path, bad))
    (n,) = db.execute("SELECT count(*) FROM core.entity").fetchone()  # type: ignore[misc]
    assert n == 0


@pytest.mark.db
def test_malformed_entity_id_is_rejected(db: psycopg.Connection, tmp_path: Path) -> None:
    bad = GOOD_ROW.replace("CN.688256,", "688256,", 1)
    with pytest.raises(SeedError, match="entity_id"):
        load_entities(db, _csv(tmp_path, bad))


@pytest.mark.db
def test_duplicate_entity_id_in_csv_is_rejected(db: psycopg.Connection, tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="重复"):
        load_entities(db, _csv(tmp_path, GOOD_ROW + GOOD_ROW))


@pytest.mark.db
def test_load_all_reports_counts_per_table(db: psycopg.Connection, tmp_path: Path) -> None:
    _csv(tmp_path, GOOD_ROW)
    (tmp_path / "entity_alias.csv").write_text(
        "alias,entity_id,alias_type,source,confidence\n寒武纪,CN.688256,short,manual,high\n",
        encoding="utf-8",
    )
    counts = load_all(db, tmp_path)
    assert counts["core.entity"] == 1
    assert counts["core.entity_alias"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/seed -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.seed'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/seed/__init__.py`：

```python
"""种子数据导入。"""
```

`src/ragdemo/seed/loader.py`：

```python
"""种子 CSV 导入：先全量校验，再整体写入。

「先校验后写入」而不是边读边写：一份 CSV 里有一行错就整份拒绝。
部分导入的种子数据比没有种子数据更糟——它看起来成功了，实际缺了一批实体，
而下游的关系导入会因为外键失败得莫名其妙。
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import psycopg

ENTITY_ID_RE = re.compile(r"^[A-Z]{2}\.[A-Za-z0-9._-]{1,32}$")
CST = timezone.utc  # 手工数据统一按 UTC 当日 00:00 记，见 docs/03-point-in-time.md §6.3
SEED_RUN_ID = "seed"


class SeedError(ValueError):
    """种子数据校验失败。"""


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _split_nodes(raw: str) -> list[str]:
    return [n for n in (s.strip() for s in raw.split("|")) if n]


def _known_at_for(valid_from: date) -> datetime:
    """手工数据的 known_at = valid_from 当日 00:00，不是 now()。"""
    return datetime.combine(valid_from, time.min, tzinfo=CST)


def _validate_entities(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=2):  # 第 1 行是表头
        eid = row["entity_id"]
        if not ENTITY_ID_RE.match(eid):
            raise SeedError(f"第 {i} 行 entity_id 格式非法: {eid!r}（应形如 CN.688256）")
        if eid in seen:
            raise SeedError(f"第 {i} 行 entity_id 重复: {eid}")
        seen.add(eid)

        nodes = _split_nodes(row["l3_node"])
        if not nodes:
            raise SeedError(f"第 {i} 行 l3_node 为空: {eid}")
        if row["primary_node"] not in nodes:
            raise SeedError(
                f"第 {i} 行 primary_node {row['primary_node']!r} 不在 l3_node {nodes} 中: {eid}"
            )
        if not row["listed_date"]:
            raise SeedError(f"第 {i} 行缺少 listed_date: {eid}（观点评分需要它剔除次新股）")

        out.append(
            {
                **row,
                "l3_node": nodes,
                "listed_date": date.fromisoformat(row["listed_date"]),
                "fiscal_year_end": int(row["fiscal_year_end"]) if row["fiscal_year_end"] else None,
            }
        )
    return out


def load_entities(conn: psycopg.Connection, csv_path: Path) -> int:
    """导入实体，同时为每个 l3_node 写一行 entity_node_membership。"""
    records = _validate_entities(_read(csv_path))
    with conn.transaction():
        for r in records:
            conn.execute(
                "INSERT INTO core.entity (entity_id, name_full, name_short, name_en,"
                " entity_type, market, tushare_code, l1_layer, l2_segment, l3_node,"
                " primary_node, hq_country, status, listed_date, currency,"
                " fiscal_year_end, notes) "
                "VALUES (%(entity_id)s,%(name_full)s,%(name_short)s,%(name_en)s,"
                " %(entity_type)s,%(market)s,%(tushare_code)s,%(l1_layer)s,%(l2_segment)s,"
                " %(l3_node)s,%(primary_node)s,%(hq_country)s,%(status)s,%(listed_date)s,"
                " %(currency)s,%(fiscal_year_end)s,%(notes)s)",
                r,
            )
            for node in r["l3_node"]:
                conn.execute(
                    "INSERT INTO core.entity_node_membership "
                    "(entity_id, l3_node, is_primary, valid_from, known_at, source,"
                    " ingest_run_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        r["entity_id"],
                        node,
                        node == r["primary_node"],
                        r["listed_date"],
                        _known_at_for(r["listed_date"]),
                        "manual:seed",
                        SEED_RUN_ID,
                    ),
                )
    return len(records)


def _load_simple(
    conn: psycopg.Connection, csv_path: Path, table: str, columns: list[str]
) -> int:
    """按列名直插的简单导入，用于别名 / 指标 / 规则等无派生逻辑的表。

    空单元格对应的列会被整个省略，而不是写入 NULL——这样数据库的 DEFAULT 才会生效。
    node_metric.direction 与 propagation_rule.lag_days 都是 NOT NULL DEFAULT，
    写 NULL 会直接违反约束。
    """
    rows = _read(csv_path)
    if not rows:
        return 0
    missing = set(columns) - set(rows[0])
    if missing:
        raise SeedError(f"{csv_path.name} 缺少列: {sorted(missing)}")
    with conn.transaction():
        for row in rows:
            present = [c for c in columns if row[c] != ""]
            placeholders = ",".join(f"%({c})s" for c in present)
            conn.execute(
                f"INSERT INTO {table} ({','.join(present)}) VALUES ({placeholders})",  # noqa: S608
                {c: row[c] for c in present},
            )
    return len(rows)


def load_aliases(conn: psycopg.Connection, csv_path: Path) -> int:
    return _load_simple(
        conn, csv_path, "core.entity_alias",
        ["alias", "entity_id", "alias_type", "source", "confidence"],
    )


def load_metrics(conn: psycopg.Connection, csv_path: Path) -> int:
    return _load_simple(
        conn, csv_path, "core.node_metric",
        ["metric_id", "l3_node", "metric_name", "metric_role", "frequency",
         "source_type", "extraction_hint", "direction", "definition", "unit"],
    )


def load_relations(conn: psycopg.Connection, csv_path: Path) -> int:
    rows = _read(csv_path)
    with conn.transaction():
        for i, row in enumerate(rows, start=2):
            if not row.get("valid_from"):
                raise SeedError(f"第 {i} 行缺少 valid_from")
            vf = date.fromisoformat(row["valid_from"])
            conn.execute(
                "INSERT INTO core.entity_relation (from_entity, to_entity, relation_type,"
                " strength, share_estimate, direction_note, evidence_type, valid_from,"
                " known_at, source, ingest_run_id, confidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    row["from_entity"], row["to_entity"], row["relation_type"],
                    row["strength"] or None, row["share_estimate"] or None,
                    row["direction_note"] or None, row.get("evidence_type") or "expert",
                    vf, _known_at_for(vf), "manual:seed", SEED_RUN_ID,
                    row.get("confidence") or "medium",
                ),
            )
    return len(rows)


def load_propagation_rules(conn: psycopg.Connection, csv_path: Path) -> int:
    return _load_simple(
        conn, csv_path, "core.propagation_rule",
        ["trigger_node", "trigger_event", "affected_node", "direction", "lag_days",
         "mechanism", "weight_field", "confidence", "author"],
    )


_LOADERS: list[tuple[str, str, Any]] = [
    ("entity.csv", "core.entity", load_entities),
    ("entity_alias.csv", "core.entity_alias", load_aliases),
    ("node_metric.csv", "core.node_metric", load_metrics),
    ("entity_relation.csv", "core.entity_relation", load_relations),
    ("propagation_rule.csv", "core.propagation_rule", load_propagation_rules),
]


def load_all(conn: psycopg.Connection, seed_dir: Path) -> dict[str, int]:
    """按依赖顺序导入 seed_dir 下存在的 CSV，返回每张表的导入行数。"""
    counts: dict[str, int] = {}
    for filename, table, fn in _LOADERS:
        path = seed_dir / filename
        if path.exists():
            counts[table] = fn(conn, path)
    return counts
```

`db/seed/README.md`：

```markdown
# 种子数据

由创始人手工维护（工作流 W9.1，见 `docs/11-sdlc.md` §3）。

| 文件 | 目标表 | 目标数量 |
|---|---|---|
| `entity.csv` | `core.entity` + `core.entity_node_membership` | 100 |
| `entity_alias.csv` | `core.entity_alias` | 约 400 |
| `node_metric.csv` | `core.node_metric` | 30 |
| `entity_relation.csv` | `core.entity_relation` | 200 |
| `propagation_rule.csv` | `core.propagation_rule` | 15 |

约定：

- `l3_node` 用 `|` 分隔多个环节；`primary_node` 必须是其中之一。
- `listed_date` 必填——观点评分要用它剔除上市不足 60 交易日的公司。
- 导入时 `known_at` 取 `valid_from` 当日 00:00，不取导入时间。
  这是一个已知的、有意接受的乐观偏差，见 `docs/03-point-in-time.md` §6.3。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/seed -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/seed tests/seed db/seed/README.md
git commit -m "feat(seed): 种子 CSV 导入，先校验后写入且 known_at 不取导入时间"
```

---

## Task 13: CLI 与 Makefile 串联

**Files:**
- Create: `src/ragdemo/cli.py`, `tests/test_cli.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Task 3 / 11 / 12 的全部公开函数
- Produces: 命令 `ragdemo db migrate` / `ragdemo db seed` / `ragdemo db check`；Makefile 目标 `db-init` / `seed` / `test-schema`

- [ ] **Step 1: 写失败的测试**

`tests/test_cli.py`：

```python
"""CLI：三个子命令存在且 check 在有违规时以非零码退出。"""
from __future__ import annotations

from click.testing import CliRunner

from ragdemo.cli import main


def test_db_subcommands_exist() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0
    for cmd in ("migrate", "seed", "check"):
        assert cmd in result.output


def test_check_requires_dsn() -> None:
    result = CliRunner().invoke(main, ["db", "check"], env={"RAGDEMO_DSN": ""})
    assert result.exit_code != 0
    assert "RAGDEMO_DSN" in result.output
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ragdemo.cli'`

- [ ] **Step 3: 写最小实现**

`src/ragdemo/cli.py`：

```python
"""ragdemo 命令行入口。"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
import psycopg

from ragdemo.db.invariants import check_point_in_time_leaks, check_schema_invariants
from ragdemo.db.migrate import migrate
from ragdemo.seed.loader import load_all

MIGRATIONS_DIR = Path("db/migrations")
SEED_DIR = Path("db/seed")


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


@click.group()
def main() -> None:
    """AI 产业链时点研究引擎。"""


@main.group()
def db() -> None:
    """数据库操作。"""


@db.command("migrate")
def db_migrate() -> None:
    """执行尚未执行的迁移。"""
    with psycopg.connect(_dsn()) as conn:
        applied = migrate(conn, MIGRATIONS_DIR)
    click.echo(f"已执行 {len(applied)} 条迁移: {applied}" if applied else "无待执行迁移")


@db.command("seed")
def db_seed() -> None:
    """导入种子数据。"""
    with psycopg.connect(_dsn()) as conn:
        counts = load_all(conn, SEED_DIR)
    for table, n in counts.items():
        click.echo(f"{table}: {n}")


@db.command("check")
def db_check() -> None:
    """跑 schema 不变量与时点泄漏自检；有问题以非零码退出。"""
    with psycopg.connect(_dsn()) as conn:
        violations = check_schema_invariants(conn)
        leaks = check_point_in_time_leaks(conn, datetime.now(UTC))

    for v in violations:
        click.echo(f"[不变量] {v}", err=True)
    for name, n in leaks.items():
        click.echo(f"[时点泄漏] {name}: {n} 行", err=True)

    if violations or leaks:
        sys.exit(1)
    click.echo("全部检查通过")
```

Makefile 追加：

```makefile
.PHONY: db-init seed test-schema

db-init: up
	ragdemo db migrate

seed:
	ragdemo db seed

test-schema:
	ragdemo db check
	pytest tests/db -v -m db
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_cli.py -v && make db-init && make test-schema`
Expected: 2 passed；`db-init` 输出已执行 6 条迁移；`test-schema` 输出「全部检查通过」且 db 测试全绿

- [ ] **Step 5: 提交**

```bash
git add src/ragdemo/cli.py tests/test_cli.py Makefile
git commit -m "feat(cli): 加入 db migrate/seed/check 与 Makefile 目标"
```

---

## Task 14: P0 验收脚本

把 [`docs/10-roadmap.md`](../../10-roadmap.md) P0 验收表的七项做成一条命令，
避免「验收靠人肉对照表格」。

**Files:**
- Create: `tests/test_p0_acceptance.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: Makefile 目标 `accept-p0`

- [ ] **Step 1: 写失败的测试**

`tests/test_p0_acceptance.py`：

```python
"""P0 验收：docs/10-roadmap.md P0 表格的七项，逐条可执行。

这个文件跑绿 = P0 可以收。跑红 = 不能进 P1，没有商量余地。
"""
from __future__ import annotations

import os

import psycopg
import pytest

DSN = os.environ.get("RAGDEMO_DSN", "postgresql://postgres:ragdemo@localhost:5432/ragdemo")

EXPECTED_COUNTS = {
    "core.entity": 100,
    "core.entity_relation": 200,
    "core.propagation_rule": 15,
    "core.node_metric": 30,
}


@pytest.fixture(scope="module")
def conn() -> psycopg.Connection:
    return psycopg.connect(DSN)


@pytest.mark.db
@pytest.mark.parametrize(("table", "expected"), sorted(EXPECTED_COUNTS.items()))
def test_seed_counts(conn: psycopg.Connection, table: str, expected: int) -> None:
    (n,) = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # type: ignore[misc]
    assert n == expected


@pytest.mark.db
def test_required_extensions_installed(conn: psycopg.Connection) -> None:
    rows = conn.execute("SELECT extname FROM pg_extension").fetchall()
    assert {"pg_search", "vector"} <= {r[0] for r in rows}


@pytest.mark.db
def test_app_read_denied_on_base_table(conn: psycopg.Connection) -> None:
    (allowed,) = conn.execute(
        "SELECT has_table_privilege('app_read','core.fin_fact','SELECT')"
    ).fetchone()  # type: ignore[misc]
    assert allowed is False


@pytest.mark.db
def test_asof_view_raises_without_as_of(conn: psycopg.Connection) -> None:
    with conn.transaction(force_rollback=True), pytest.raises(
        psycopg.errors.InvalidParameterValue
    ):
        conn.execute("SELECT count(*) FROM asof.fin_fact").fetchone()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_p0_acceptance.py -v -m db`
Expected: FAIL — 种子计数为 0（创始人的 W9.1 数据尚未录入）

- [ ] **Step 3: 补齐使其通过**

这一步**不是写代码**，是等 W9.1 的种子数据到位并导入：

```bash
# 创始人把 5 份 CSV 放进 db/seed/ 后
make db-init && make seed
```

若某张表计数不符，看 `ragdemo db seed` 的输出定位是哪份 CSV 少了行，
回到创始人侧补齐。**不要改 `EXPECTED_COUNTS` 去迁就数据**——
那等于把验收标准改成「有多少算多少」。

- [ ] **Step 4: 跑测试确认通过**

Run: `make accept-p0`
Expected: 7 passed

Makefile 追加：

```makefile
.PHONY: accept-p0

accept-p0:
	pytest tests/test_p0_acceptance.py -v -m db
```

- [ ] **Step 5: 提交**

```bash
git add tests/test_p0_acceptance.py Makefile
git commit -m "test: P0 验收标准做成可执行测试"
```

---

## Self-Review

**Spec 覆盖检查**（对照 [`docs/11-sdlc.md`](../../11-sdlc.md) §3 的 W0.1–W0.6）：

| 工作流 | 任务 | 覆盖 |
|---|---|---|
| W0.1 工具链 | Task 1 | ✅ |
| W0.2 容器 | Task 2 | ✅ |
| W0.3 迁移 + DDL | Task 3–8 | ✅ |
| W0.4 时点安全层 | Task 9, 10 | ✅ |
| W0.5 不变量 + 泄漏自检 | Task 11 | ✅ |
| W0.6 种子导入 | Task 12 | ✅ |
| 阶段验收（[10](../../10-roadmap.md) P0 七项） | Task 14 | ✅ |

**类型一致性检查**：`migrate()` 在 Task 3 定义、Task 4–9 与 Task 13 使用，签名一致；
`check_schema_invariants` / `check_point_in_time_leaks` 在 Task 11 定义、Task 13 使用，
签名一致；`load_all` 在 Task 12 定义、Task 13 使用，签名一致；
`as_of_session` 在 Task 10 定义、Task 11 的测试使用，签名一致。

**已知的外部阻塞**：Task 14 依赖创始人工作流 W9.1 的种子数据。
这是[`docs/11-sdlc.md`](../../11-sdlc.md) §3 强调「W9.1 必须第 1 天启动」的原因——
若它拖到 Task 13 做完才开始，P0 会因为等数据而空转。

---

## 完成 P0 之后

1. 在 [`docs/10-roadmap.md`](../../10-roadmap.md) P0 的 checklist 上打勾。
2. 把 P0 实测中发现的任何与文档不符之处**回写文档**
   （`pg_search` 行为、分词器可用性、镜像版本）——文档是契约。
3. 按 [`docs/11-sdlc.md`](../../11-sdlc.md) §8 编写 P1 的实施计划，
   放在 `docs/superpowers/plans/<日期>-p1-foundation.md`。
