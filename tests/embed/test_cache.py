"""嵌入缓存：主键三列，跨模型与跨用户不串。"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.embed.base import content_key
from ragdemo.embed.cache import EmbeddingCache
from ragdemo.embed.mock import MockEmbedder
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


@pytest.mark.db
def test_put_then_get_roundtrips(conn: psycopg.Connection) -> None:
    cache = EmbeddingCache(conn, model="bge-m3")
    key = content_key("智能计算收入 12,340 万元")
    vector = MockEmbedder().embed(["智能计算收入 12,340 万元"])[0]
    cache.put_many({key: vector})
    got = cache.get_many([key])
    assert len(got[key]) == len(vector)
    assert got[key][:3] == pytest.approx(vector[:3])


@pytest.mark.db
def test_miss_returns_no_entry(conn: psycopg.Connection) -> None:
    assert EmbeddingCache(conn, model="bge-m3").get_many([content_key("没存过")]) == {}


@pytest.mark.db
def test_same_content_different_model_does_not_collide(conn: psycopg.Connection) -> None:
    """换模型后读到旧模型的向量不会报错，只会让检索质量莫名下降。"""
    key = content_key("同一段文本")
    EmbeddingCache(conn, model="bge-m3").put_many({key: MockEmbedder().embed(["a"])[0]})
    assert EmbeddingCache(conn, model="qwen3-embedding").get_many([key]) == {}


@pytest.mark.db
def test_private_content_does_not_leak_into_shared_cache(conn: psycopg.Connection) -> None:
    """09 §3.3：私有内容进共享缓存，可通过哈希命中探测别人上传过什么。"""
    key = content_key("用户私有材料")
    EmbeddingCache(conn, model="bge-m3", owner_user="u1").put_many(
        {key: MockEmbedder().embed(["x"])[0]}
    )
    assert EmbeddingCache(conn, model="bge-m3").get_many([key]) == {}
    assert EmbeddingCache(conn, model="bge-m3", owner_user="u2").get_many([key]) == {}
    assert EmbeddingCache(conn, model="bge-m3", owner_user="u1").get_many([key])


@pytest.mark.db
def test_put_many_is_idempotent(conn: psycopg.Connection) -> None:
    cache = EmbeddingCache(conn, model="bge-m3")
    key = content_key("重复写入")
    vector = MockEmbedder().embed(["y"])[0]
    cache.put_many({key: vector})
    cache.put_many({key: vector})
    (n,) = conn.execute("SELECT count(*) FROM core.embedding_cache").fetchone()  # type: ignore[misc]
    assert n == 1
