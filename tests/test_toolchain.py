"""工具链自检：两个包可导入，关键约束已配置，依赖方向没有被违反。"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMBER_PYPROJECTS = (
    REPO_ROOT / "packages" / "ragdemo-core" / "pyproject.toml",
    REPO_ROOT / "packages" / "ragdemo" / "pyproject.toml",
)
CORE_SRC = REPO_ROOT / "packages" / "ragdemo-core" / "src" / "ragdemo_core"


def _load(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_both_packages_importable() -> None:
    import ragdemo
    import ragdemo_core

    assert ragdemo.__name__ == "ragdemo"
    assert ragdemo_core.__name__ == "ragdemo_core"


def test_python_floor_is_311_in_every_member() -> None:
    for pyproject in MEMBER_PYPROJECTS:
        cfg = _load(pyproject)
        project = cfg["project"]
        assert isinstance(project, dict)
        assert project["requires-python"] == ">=3.11", pyproject


def test_mypy_is_strict() -> None:
    cfg = _load(REPO_ROOT / "pyproject.toml")
    tool = cfg["tool"]
    assert isinstance(tool, dict)
    mypy = tool["mypy"]
    assert isinstance(mypy, dict)
    assert mypy["strict"] is True


def test_workspace_root_declares_both_members() -> None:
    cfg = _load(REPO_ROOT / "pyproject.toml")
    tool = cfg["tool"]
    assert isinstance(tool, dict)
    uv = tool["uv"]
    assert isinstance(uv, dict)
    workspace = uv["workspace"]
    assert isinstance(workspace, dict)
    assert workspace["members"] == ["packages/*"]


def test_core_package_does_not_import_business_layer() -> None:
    """docs/01-architecture.md §5：依赖只能自上而下。

    底层包 import 业务层会造成循环，并让「工具外壳层不可绕过」的约束失效。
    这条测试把架构约定变成包边界上的物理检查。
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}"
        for path in CORE_SRC.rglob("*.py")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.startswith(("import ragdemo.", "from ragdemo.", "import ragdemo "))
        or line.strip() == "import ragdemo"
    ]
    assert offenders == []
