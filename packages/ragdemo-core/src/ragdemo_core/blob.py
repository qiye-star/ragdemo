"""对象存储抽象。

P1 用本地文件系统，P4 接 MinIO（docs/01-architecture.md §4）。
放在 core 包里是因为它与 db 同层：接入侧存原始 PDF、解析侧存解析产物、
备份侧存 pg_dump，三处都要用，而它不含任何业务语义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


class BlobNotFound(FileNotFoundError):
    """`key` 在存储里不存在。

    这是 `BlobStore.get()` 的契约异常，不是某个实现的偶然产物：
    `LocalBlobStore` 以前直接让 `Path.read_bytes()` 的 `FileNotFoundError`
    冒出去，调用方（`ragdemo/ingest/assets_docs.py` 的 `prepare_documents`）
    捕的其实是这个实现细节，而不是一个有意设计的契约——P4 的 MinIO 适配器
    抛的是供应商自己的 `NoSuchKey`，不是 `FileNotFoundError`，届时那个
    catch 会静默失效，刚补上的洞又开了一次。子类化 `FileNotFoundError`
    是为了不破坏任何已经在捕获它的既有调用方。
    """


@runtime_checkable
class BlobStore(Protocol):
    def exists(self, key: str) -> bool: ...

    def get(self, key: str) -> bytes:
        """`key` 不存在时必须抛 `BlobNotFound`（或其子类）——不是任意异常，
        也不是某个实现恰好抛出的原生异常。"""
        ...

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
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFound(key) from exc

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + 原子替换。直接写的话，进程在中途死掉会留下半个 JSON，
        # 而 exists() 会把它当成缓存命中——那份坏数据会一直被读下去。
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key
