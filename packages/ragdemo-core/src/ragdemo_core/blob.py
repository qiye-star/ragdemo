"""对象存储抽象。

P1 用本地文件系统，P4 接 MinIO（docs/01-architecture.md §4）。
放在 core 包里是因为它与 db 同层：接入侧存原始 PDF、解析侧存解析产物、
备份侧存 pg_dump，三处都要用，而它不含任何业务语义。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    def exists(self, key: str) -> bool: ...
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, data: bytes) -> str: ...


class LocalBlobStore:
    """本地文件系统实现。key 里的 '/' 映射成目录层级。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # key 由固定前缀 + sha256 拼出，理论上安全；但路径穿越的后果是
        # 静默写到 root 外面，挡一下的成本远低于事后发现。
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError(f"key 越出存储根目录: {key!r}")
        return resolved

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + 原子替换。直接写的话，进程在中途死掉会留下半个 JSON，
        # 而 exists() 会把它当成缓存命中——那份坏数据会一直被读下去。
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key
