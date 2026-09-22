"""源码扫描守卫：这些约束靠正则表达式而不是运行时检查来保证——一旦有人
在 `queries/*.py` 里手滑加了一列 `content`，或者在业务层某处 import 了
`ragdemo.api`，这里应该先红，而不是等到真出现数据泄漏或依赖方向倒挂才
被发现。全部不需要数据库，也不 import 被扫描的模块（只读源码文本），
不给这几条测试增加任何运行时开销。
"""

from __future__ import annotations

import re
from pathlib import Path

QUERIES_DIR = Path("packages/ragdemo/src/ragdemo/api/queries")
API_DIR = Path("packages/ragdemo/src/ragdemo/api")
RAGDEMO_SRC = Path("packages/ragdemo/src/ragdemo")

_BITEMPORAL_TABLES = (
    "document",
    "doc_block",
    "fin_fact",
    "price_daily",
    "entity_relation",
    "entity_node_membership",
    "event",
)

# 隔离探针的 base_table_denied 分支故意查 core.doc_block，用来验证
# 约束 5 被绕过时数据库会不会真的拒绝（42501）——这是唯一豁免的一处，
# 不是一个口子：豁免只认这一个精确的 (文件, 表) 组合。
_ALLOWED_BASE_TABLE_HITS = {("isolation.py", "doc_block")}

# 非 asof 运维/配置表白名单——它们不在 core.bitemporal_registry 里，
# 没有 asof 视图可查，"只查 asof 视图" 对它们不可满足而不是被违反
# （web-diagnostic-ui 计划裁决 2）。新增一张表必须显式加进这里，
# 不能靠 ALL TABLES 式的隐式放行。
_NON_ASOF_ALLOWLIST = {
    "core.parse_tier_policy",
    "core.parse_retry_queue",
    "quality.quality_metric",
}

_FORBIDDEN_TEXT_COLUMNS = ("content", "title", "table_html", "preview", "note")


def _query_files() -> list[Path]:
    files = sorted(QUERIES_DIR.glob("*.py"))
    assert files, f"{QUERIES_DIR} 下没有扫到任何文件——测试本身可能失效了"
    return files


_DOCSTRING_RE = re.compile(r'""".*?"""', re.DOTALL)


def _code_only(text: str) -> str:
    """去掉三引号文档字符串与整行注释，只留代码——避免文档字符串里
    举例提到的字面量（比如本文件互相解释对方约束时引用的 SQL 片段）
    误触发扫描。"""
    without_docstrings = _DOCSTRING_RE.sub("", text)
    return "\n".join(
        line for line in without_docstrings.splitlines() if not line.strip().startswith("#")
    )


def test_no_query_module_uses_select_star() -> None:
    pattern = re.compile(r"select\s+\*", re.IGNORECASE)
    for path in _query_files():
        code = _code_only(path.read_text(encoding="utf-8"))
        assert not pattern.search(code), (
            f"{path} 里出现了 SELECT *——asof.doc_block 有 embedding vector(1024)，"
            "会把 1024 维向量整列带进响应"
        )


def test_no_query_touches_a_bitemporal_base_table() -> None:
    pattern = re.compile(rf"\b(?:FROM|JOIN)\s+core\.({'|'.join(_BITEMPORAL_TABLES)})\b")
    hits = {
        (path.name, m.group(1))
        for path in _query_files()
        for m in pattern.finditer(path.read_text(encoding="utf-8"))
    }
    assert hits == _ALLOWED_BASE_TABLE_HITS, (
        f"发现未豁免的基表访问 {hits - _ALLOWED_BASE_TABLE_HITS}——"
        "约束 5「只查 asof 视图」在这里被绕过了"
    )


def test_non_asof_tables_are_all_in_the_allowlist() -> None:
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+((?:core|quality)\.\w+)\b")
    found = {
        m.group(1)
        for path in _query_files()
        for m in pattern.finditer(path.read_text(encoding="utf-8"))
    }
    # core.doc_block 是探针故意查的基表（上一条测试已经守着它），
    # 不算"非 asof 运维表白名单"的一员，这里单独排掉。
    found.discard("core.doc_block")
    assert found, "一个非 asof 表引用都没扫到——白名单机制本身可能失效了"
    assert found <= _NON_ASOF_ALLOWLIST, (
        f"发现不在白名单里的非 asof 表 {found - _NON_ASOF_ALLOWLIST}——"
        "新表必须显式加进 _NON_ASOF_ALLOWLIST，不能隐式放行"
    )


def test_probe_sql_selects_no_text_columns() -> None:
    text = (QUERIES_DIR / "isolation.py").read_text(encoding="utf-8")
    for forbidden in _FORBIDDEN_TEXT_COLUMNS:
        assert forbidden not in text, (
            f"isolation.py 里出现了疑似文本列 {forbidden!r}——"
            "隔离探针只应该返回聚合计数，一个文本列都不该取"
        )


def test_only_one_place_sets_the_as_of_guc_directly() -> None:
    """`ragdemo_core.db.session.as_of_session` 是全仓库唯一正确设置
    `app.as_of` 的入口。`queries/isolation.py::_missing_as_of_branch`
    故意手写一次 `set_config('app.as_of', '', true)` 来测试"不设 as_of
    会报错"这个分支——这是唯一豁免的一处，不是一个可以效仿的写法。
    """
    pattern = re.compile(r"set_config\(\s*['\"]app\.as_of['\"]")
    hits: dict[str, int] = {}
    for path in sorted(API_DIR.rglob("*.py")):
        count = len(pattern.findall(_code_only(path.read_text(encoding="utf-8"))))
        if count:
            hits[path.relative_to(API_DIR).as_posix()] = count
    assert hits == {"queries/isolation.py": 1}, (
        f"api/ 下手写 set_config('app.as_of' 的位置是 {hits}，"
        "预期只有 queries/isolation.py 里那一处故意的测试分支"
    )


def test_nothing_outside_api_and_cli_imports_ragdemo_api() -> None:
    """`api/` 是最上层——只允许 CLI 的 `serve` 命令（作为进程入口）向下
    import 它，任何业务层模块（ingest/parse/retrieval/quality/seed/...）
    都不该反过来依赖诊断接口。
    """
    pattern = re.compile(r"^\s*(?:import\s+ragdemo\.api|from\s+ragdemo\.api\b)", re.MULTILINE)
    offenders = []
    for path in RAGDEMO_SRC.rglob("*.py"):
        if API_DIR in path.parents:
            continue  # api/ 自己内部互相 import 天经地义
        if path.name == "cli.py" and path.parent == RAGDEMO_SRC:
            continue  # serve 命令的合法入口，见 cli.py::serve 的惰性 import
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path))
    assert offenders == [], f"以下文件违反依赖方向、import 了 ragdemo.api: {offenders}"
