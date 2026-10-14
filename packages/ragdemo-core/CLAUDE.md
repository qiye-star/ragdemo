# packages/ragdemo-core — 底层包：迁移、时点会话、Schema 不变量、对象存储

## 这个包做什么
整个系统的最底层，只依赖 `psycopg`。它提供三件与业务无关的基础设施：
SQL 迁移执行器、把 `as_of` 绑到事务上的会话、以及守护时点语义的 Schema 不变量/泄漏自检；
外加一个对象存储抽象（`BlobStore`）。接入、解析、检索、API 都建在它上面。
源码在 `src/ragdemo_core/`，没有 CLI，没有业务语义。

## 关键模块
| 文件 | 职责 |
|---|---|
| `src/ragdemo_core/blob.py` | `BlobStore` Protocol + `LocalBlobStore`（本地文件系统）+ `BlobNotFound` |
| `src/ragdemo_core/db/` | 迁移、`as_of_session`、不变量，见 `src/ragdemo_core/db/CLAUDE.md` |

## 对外入口
- 被 `packages/ragdemo/` 广泛 import：`from ragdemo_core.db.session import as_of_session`、
  `from ragdemo_core.blob import BlobStore, LocalBlobStore, BlobNotFound`、
  `from ragdemo_core.db.migrate import migrate`、`from ragdemo_core.db.invariants import ...`。
- **本包不得 import `ragdemo`（业务层）**，依赖方向见 `docs/01-architecture.md` §5；
  `tests/test_toolchain.py::test_core_package_does_not_import_business_layer` 逐行扫描 `src/ragdemo_core/` 守着。
- 包内不得加任何业务概念（实体、公告、指标等）。判断标准：它是否能不改动就被另一个项目复用。

## 本包的硬规则
- `BlobStore.get()` 对不存在的 key **必须抛 `BlobNotFound`**（`FileNotFoundError` 子类），
  不得抛实现自己的异常；调用方（`ingest/assets_docs.py`）依赖这个契约。P4 换 MinIO 适配器时同样要遵守。
- `LocalBlobStore._path` 拒绝越出 root 的 key；`put` 走「`.part` 临时文件 + 原子替换」，
  不得改成直接写（半个文件会被 `exists()` 当成缓存命中）。
- `as_of_session` 拒绝 naive datetime，且用事务级 `set_config(..., true)`；不得改成会话级 SET（连接池会泄漏 as_of）。

## 怎么测
在仓库根目录运行（迁移路径 `db/migrations` 是相对 cwd 的）：
- 免费、无需数据库：`pytest tests/test_blob.py tests/test_toolchain.py`
- 需要数据库（`@pytest.mark.db`）：`pytest tests/db`。`tests/conftest.py` 的 `temp_db` 夹具连
  `RAGDEMO_ADMIN_DSN`（默认 `postgresql://postgres:ragdemo@127.0.0.1:5433/postgres`，即 compose 起的 ParadeDB），
  每个测试新建随机名 `t_xxxx` 空库、结束时 `DROP DATABASE ... WITH (FORCE)`，不碰业务库。
- 均不产生付费调用。

## 常见坑
- DSN 用 `127.0.0.1` 而非 `localhost`：Windows 上 localhost 先解析到 `::1`，会卡满连接超时（见 `tests/conftest.py` 注释）。
- 没有 `tests/ragdemo_core/` 目录，测试按功能分散在 `tests/db/`、`tests/test_blob.py`。
