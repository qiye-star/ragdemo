"""静态守卫：每个真实取数点都经过原始响应留存（CLAUDE.md §1.5、docs/04-ingestion.md §2.1）。

规则：业务包里任何一个函数，只要调用了"向供应商取数"的方法，同一个函数里就必须
也调用 `record_snapshot` / `record_blob_snapshot` / `record_parse_snapshots` 之一。

取数方法（按调用名识别）：
- 适配器：`fetch` / `fetch_daily` / `list_documents` / `fetch_full_text` / `call_tool`
- 解析供应商：`prepare_documents(...)`（内部调 TextIn）与接收者名为 `parser` 的 `.parse(...)`

适配器自己的实现（`adapters/`）不在扫描范围——它们就是被包裹的那一层。
白名单只放"确实不发起供应商调用"的点，并写明理由。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "ragdemo" / "src" / "ragdemo"
EXCLUDED_DIRS = {"adapters"}

FETCH_METHODS = {"fetch", "fetch_daily", "list_documents", "fetch_full_text", "call_tool"}
SNAPSHOT_CALLS = {"record_snapshot", "record_blob_snapshot", "record_parse_snapshots"}

# (模块相对路径, 函数名) → 理由
ALLOWLIST: dict[tuple[str, str], str] = {
    ("ingest/assets_docs.py", "prepare_documents"): (
        "刻意保持不碰数据库的纯函数；它产出的 PreparedDocument 带 billed_pages，"
        "由每个调用方紧接着调 record_parse_snapshots 留底（本守卫对调用方生效）"
    ),
    ("ingest/cli.py", "docs_rechunk"): (
        "只走缓存命中路径：调用 parser.parse() 之前已确认解析产物在 blob 里，"
        "不会发起计费调用（见该命令 docstring）"
    ),
    ("ingest/edgar_docs.py", "list_filings"): (
        "dry run 专用：只列申报、不写库；--yes 走 ingest_edgar_filings，那里留底"
    ),
}


def _call_name(node: ast.Call) -> tuple[str | None, str | None]:
    """返回 (接收者名, 方法/函数名)。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        return receiver, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _is_fetch(node: ast.Call) -> bool:
    receiver, name = _call_name(node)
    if name in FETCH_METHODS and receiver is not None:
        return True
    if name == "prepare_documents" and receiver is None:
        return True
    return name == "parse" and receiver == "parser"


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _own_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """函数体内的调用，不含嵌套函数里的（嵌套函数单独检查）。"""
    calls: list[ast.Call] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


def uncovered_fetch_sites(source: str) -> list[str]:
    """返回源码中"取了数却没留快照"的函数名。"""
    missing: list[str] = []
    for fn in _functions(ast.parse(source)):
        calls = _own_calls(fn)
        if any(_is_fetch(c) for c in calls) and not any(
            _call_name(c)[1] in SNAPSHOT_CALLS for c in calls
        ):
            missing.append(fn.name)
    return missing


def _scan() -> dict[str, list[str]]:
    report: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.split("/", 1)[0] in EXCLUDED_DIRS:
            continue
        missing = [
            name
            for name in uncovered_fetch_sites(path.read_text(encoding="utf-8"))
            if (rel, name) not in ALLOWLIST
        ]
        if missing:
            report[rel] = missing
    return report


def test_every_fetch_site_records_a_snapshot() -> None:
    report = _scan()
    assert report == {}, (
        "以下函数向供应商取数却没有留存原始响应（CLAUDE.md §1.5）——"
        "在同一函数里调 record_snapshot / record_blob_snapshot / record_parse_snapshots：\n"
        + "\n".join(f"  {rel}: {names}" for rel, names in report.items())
    )


def test_allowlist_entries_still_exist() -> None:
    """白名单项若对应的函数已经不在了，豁免就成了无主的，必须同步删掉。"""
    for rel, name in ALLOWLIST:
        source = (PACKAGE / rel).read_text(encoding="utf-8")
        assert name in {f.name for f in _functions(ast.parse(source))}, (rel, name)


def test_the_scan_actually_finds_fetch_sites() -> None:
    """扫描器至少要看见一批取数点，否则它可能因为规则写错而什么都没检查。"""
    seen = 0
    for path in PACKAGE.rglob("*.py"):
        if path.relative_to(PACKAGE).parts[0] in EXCLUDED_DIRS:
            continue
        for fn in _functions(ast.parse(path.read_text(encoding="utf-8"))):
            if any(_is_fetch(c) for c in _own_calls(fn)):
                seen += 1
    assert seen >= 6, f"只找到 {seen} 个取数函数——规则可能失效了"


# --- 守卫本身的自测 ---------------------------------------------------------------


def test_detects_fetch_without_snapshot() -> None:
    src = "def pull(adapter, ctx):\n    for raw in adapter.fetch(ctx):\n        use(raw)\n"
    assert uncovered_fetch_sites(src) == ["pull"]


def test_detects_vendor_parse_without_snapshot() -> None:
    src = "def reparse(parser, data):\n    return parser.parse(data)\n"
    assert uncovered_fetch_sites(src) == ["reparse"]


def test_accepts_fetch_with_snapshot() -> None:
    src = (
        "def pull(adapter, ctx, conn, blob):\n"
        "    for raw in adapter.fetch(ctx):\n"
        "        record_snapshot(conn, raw, ingest_run_id='r', blob=blob)\n"
    )
    assert uncovered_fetch_sites(src) == []


def test_adapter_parse_of_raw_is_not_a_fetch() -> None:
    """`adapter.parse(raw)` 只是解析已拿到的响应，不是一次取数。"""
    src = "def normalize(adapter, raw):\n    return list(adapter.parse(raw))\n"
    assert uncovered_fetch_sites(src) == []
