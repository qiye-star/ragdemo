"""静态守卫：`ingested_at` 不得出现在任何比较/过滤表达式里（docs/03-point-in-time.md §1.2）。

`ingested_at` 是"本系统何时写入这一行"，`known_at` 才是"现实中何时可被获知"。
回填历史数据时两者可以相差数年；拿 `ingested_at` 做 `as_of` 过滤，回测会安静地
看到一个空世界，或者更糟——看到它当时不该看到的数据。这种错误不会报错，只能靠
静态检查在合并前拦住。

规则：`ingested_at` 作为比较运算的一侧出现即违规——
- SQL 文本（`packages/` 下 Python 字符串字面量 + `db/migrations/*.sql`）里的
  `ingested_at <op> ...` / `... <op> ingested_at` / `ingested_at BETWEEN`；
- Python 代码里操作数是 `ingested_at`（名字、属性、下标键）的比较表达式。

合法用法不受影响：建表 DDL（`DEFAULT now()`）、接入延迟指标里的
`ingested_at - publish_at`（算术，不是过滤）、注释与 docstring。
唯一的白名单是专门**检测**这个缺陷的不变量查询本身。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_DIRS = [ROOT / "packages"]
SQL_DIRS = [ROOT / "db" / "migrations"]

# 白名单：(文件相对路径, 必须同时出现在该 SQL 里的标记)。只放"检测缺陷"的查询。
ALLOWLIST: list[tuple[str, str]] = [
    (
        "packages/ragdemo-core/src/ragdemo_core/db/invariants.py",
        "known_at = ingested_at",
    ),
]

_OPS = r"(?:<=|>=|<>|!=|=|<|>)"
_COL = r"(?:\b\w+\.)?\bingested_at\b"
_SQL_VIOLATION = re.compile(
    rf"{_COL}\s*(?:{_OPS}|\bBETWEEN\b|\bIS\s+(?:NOT\s+)?DISTINCT\s+FROM\b)"
    rf"|{_OPS}\s*{_COL}",
    re.IGNORECASE,
)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def sql_violations(sql: str) -> list[str]:
    """返回 SQL 文本中把 `ingested_at` 当比较操作数的片段。"""
    text = _SQL_BLOCK_COMMENT.sub(" ", _SQL_LINE_COMMENT.sub(" ", sql))
    return [m.group(0) for m in _SQL_VIOLATION.finditer(text)]


def _is_ingested_at(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "ingested_at"
    if isinstance(node, ast.Attribute):
        return node.attr == "ingested_at"
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "ingested_at"
    return False


def _sql_texts(tree: ast.AST) -> list[str]:
    """Python 源码里所有可能是 SQL 的字符串：普通字面量（相邻字面量已由解析器
    合并）与 f-string 的常量部分拼起来的文本。"""
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            texts.append(
                " ".join(
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            )
    return texts


def python_violations(source: str) -> list[str]:
    """返回 Python 源码中的违规：SQL 字面量里的比较，以及 Python 比较表达式。"""
    tree = ast.parse(source)
    found: list[str] = []
    for text in _sql_texts(tree):
        found.extend(sql_violations(text))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_is_ingested_at(o) for o in operands):
                found.append(ast.unparse(node))
    return found


def _allowed(rel: str, snippet_source: str) -> bool:
    return any(rel == path and marker in snippet_source for path, marker in ALLOWLIST)


def _scan() -> dict[str, list[str]]:
    report: dict[str, list[str]] = {}
    for base in PY_DIRS:
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            hits: list[str] = []
            for text in _sql_texts(tree):
                if sql_violations(text) and not _allowed(rel, text):
                    hits.extend(sql_violations(text))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and any(
                    _is_ingested_at(o) for o in [node.left, *node.comparators]
                ):
                    hits.append(ast.unparse(node))
            if hits:
                report[rel] = hits
    for base in SQL_DIRS:
        for path in sorted(base.glob("*.sql")):
            rel = path.relative_to(ROOT).as_posix()
            hits = sql_violations(path.read_text(encoding="utf-8"))
            if hits:
                report[rel] = hits
    return report


def test_no_ingested_at_in_filters_anywhere() -> None:
    report = _scan()
    assert report == {}, (
        "ingested_at 出现在比较/过滤表达式里（docs/03-point-in-time.md §1.2）——"
        "时点过滤必须用 known_at。违规位置：\n"
        + "\n".join(f"  {path}: {hits}" for path, hits in report.items())
    )


def test_allowlisted_detector_query_still_exists() -> None:
    """白名单项若已不存在（查询被删改），白名单就成了一个无主的豁免，必须同步删掉。"""
    for rel, marker in ALLOWLIST:
        assert marker in (ROOT / rel).read_text(encoding="utf-8"), (rel, marker)


# --- 守卫本身的自测：确认它真能拦住、也不误伤 -----------------------------------


def test_detects_as_of_filter_on_ingested_at() -> None:
    assert sql_violations("SELECT * FROM core.fin_fact WHERE ingested_at <= %(as_of)s")
    assert sql_violations("... AND d.ingested_at > now() - interval '1 day'")
    assert sql_violations("WHERE %(as_of)s >= ingested_at")
    assert sql_violations("WHERE ingested_at BETWEEN %s AND %s")
    assert sql_violations("WHERE ingested_at IS NOT DISTINCT FROM %s")


def test_detects_python_comparison_on_ingested_at() -> None:
    assert python_violations("ok = row.ingested_at <= as_of\n")
    assert python_violations("ok = r['ingested_at'] < cutoff\n")
    assert python_violations("q = 'SELECT 1 FROM t WHERE ' 'ingested_at <= %s'\n")
    assert python_violations("q = f'SELECT 1 FROM {t} WHERE ingested_at <= {x}'\n")


def test_allows_ddl_arithmetic_and_comments() -> None:
    assert not sql_violations("  ingested_at   timestamptz NOT NULL DEFAULT now(),")
    assert not sql_violations(
        "percentile_cont(0.5) WITHIN GROUP"
        " (ORDER BY EXTRACT(EPOCH FROM (ingested_at - publish_at)))"
    )
    assert not sql_violations("-- 这里故意不用 ingested_at <= as_of 做收窄")
    assert not sql_violations("/* ingested_at = now() 只是说明 */ SELECT 1")
    assert not python_violations('"""ingested_at 只用于运维排查"""\nx = 1\n')
