"""对象存储抽象。写入必须原子——半个 JSON 会让缓存命中一份坏数据。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragdemo_core.blob import BlobNotFound, BlobStore, LocalBlobStore


def test_put_then_get_roundtrip(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    key = store.put("parse/textin/abc/deadbeef.json", b'{"a":1}')
    assert key == "parse/textin/abc/deadbeef.json"
    assert store.get(key) == b'{"a":1}'


def test_exists_is_false_before_put(tmp_path: Path) -> None:
    assert LocalBlobStore(tmp_path).exists("parse/textin/abc/deadbeef.json") is False


def test_nested_key_creates_directories(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("a/b/c/d.json", b"x")
    assert (tmp_path / "a" / "b" / "c" / "d.json").read_bytes() == b"x"


def test_no_partial_file_is_left_behind(tmp_path: Path) -> None:
    """写入走临时文件 + 原子替换，目录里不留 .part。"""
    store = LocalBlobStore(tmp_path)
    store.put("x/y.json", b"payload")
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["y.json"]


def test_key_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    """key 由 sha256 拼出，但挡一下路径穿越——写到 root 外面去是静默的。"""
    with pytest.raises(ValueError, match="越出存储根目录"):
        LocalBlobStore(tmp_path).put("../../etc/passwd", b"x")


def test_local_store_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalBlobStore(tmp_path), BlobStore)


def test_missing_key_raises_blob_not_found(tmp_path: Path) -> None:
    """`BlobNotFound` 是 `BlobStore.get()` 的契约异常，不是
    `Path.read_bytes()` 恰好抛出的实现细节——调用方（`prepare_documents`）
    应该捕这个类型，而不是绑定在某一个实现上。"""
    with pytest.raises(BlobNotFound):
        LocalBlobStore(tmp_path).get("does/not/exist.json")


def test_blob_not_found_is_still_a_file_not_found_error(tmp_path: Path) -> None:
    """子类化 `FileNotFoundError`：已经在捕获它的既有调用方不能被这个改动
    悄悄破坏。"""
    with pytest.raises(FileNotFoundError):
        LocalBlobStore(tmp_path).get("does/not/exist.json")
