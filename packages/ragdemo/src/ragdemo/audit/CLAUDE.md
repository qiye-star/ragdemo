# packages/ragdemo/src/ragdemo/audit — tool_call_log 与审计哈希链（P2 占位，尚无实现）

## 这个包做什么
设计上负责向 `audit.tool_call_log` 追加记录并维护单向哈希链、每日校验链完整性（`docs/09-compliance-security.md` §4.3）。
**现状：只有一行 docstring 的 `__init__.py`，没有 Python 代码。** 数据库侧表结构已存在
（`db/migrations/005_opinions_evals_audit.sql`；`006` 对应用角色 `REVOKE UPDATE, DELETE`）。

## 关键模块
暂无。

## 对外入口
将来由 `tools/` 的工具外壳在每次调用后调用写入函数；校验由定时任务调用。

## 本包的硬规则（实现时必须守）
- 表只允许 INSERT；`hash = sha256(prev_hash + canonical_json(指定字段))`，链起点 `prev_hash = "0" * 64`（`docs/09-compliance-security.md` §4.3）。
- 写入必须单一序列化（进程内锁或 advisory lock），否则并发会让链分叉。
- 日志里不得出现密钥；`params` 入库前同样剥离认证字段（复用 `adapters/secrets.py::strip_secrets`）。
- 记录里的 `as_of` 是该次调用使用的时点，不是写入时间；`ingested_at` 类时间不得参与 `as_of` 过滤（CLAUDE.md §1.1）。

## 怎么测
暂无测试；实现时放 `tests/audit/`，需数据库的用 `@pytest.mark.db` 与 `temp_db` 夹具，并覆盖篡改一行后校验能定位断链点。

## 常见坑
- 表由迁移 005 建；改表结构要新增迁移，不得手工改（CLAUDE.md §3）。
- 并发写链、`canonical_json` 的数值格式/字段顺序稳定性是最容易出错的两处。
