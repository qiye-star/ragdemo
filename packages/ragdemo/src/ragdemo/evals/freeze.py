"""评测冻结快照（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 H）。

没有冻结快照，两周后重跑评测，`recall@10` 的变化分不清是检索改好了还是
语料变了——语料每天都在长（新公告持续入库），`chunking_version`/
`embedding_version` 也可能因为切块参数调整或重新嵌入而变。

固定 `as_of` 已经能保证 bitemporal 意义上的"这个时点看到的内容不会变"
（`asof.doc_block` 的 `known_at <= as_of AND (superseded_at IS NULL OR
superseded_at > as_of)` 判定天然可复现，见 docs/03-point-in-time.md）——
但这里仍然把实际观测到的 `block_id` 集合与 `chunking_version`/
`embedding_version` 落一份快照存文件，而不是只信赖"重放 as_of 就够了"这个
推理：块级重解析（阶段 G 的 C 档）用的是"沿用原 known_at"的更正模型，
存在多个版本在同一个历史 as_of 下同时满足可见性判定的边界情况（这是本次
在写这个模块时注意到、但明确不在阶段 H 范围内修的一处既有设计问题，已经
另外记录跟进——见本模块调用方对应的说明）。落一份具体的快照，"当时到底
看到了哪些块"就有据可查，不必依赖对 bitemporal 语义的信任推断。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg


@dataclass(frozen=True)
class EvalFreeze:
    as_of: str  # ISO 8601，序列化友好；datetime 对象由调用方按需解析
    block_ids: tuple[int, ...]
    chunking_versions: tuple[str, ...]
    embedding_versions: tuple[str, ...]
    created_at: str

    def as_of_dt(self) -> datetime:
        return datetime.fromisoformat(self.as_of)


def create_freeze(conn: psycopg.Connection, as_of: datetime) -> EvalFreeze:
    """按 `as_of` 查一遍 `asof.doc_block`，把实际观测到的块与版本落成快照。

    `set_config('app.as_of', ..., true)` 的 `is_local=true` 把 GUC 限定在
    当前事务内——与 `quality/replay.py::_replay_hash` 同一个做法，避免这次
    设置泄漏到这条连接上后续的其他查询。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"as_of 必须带时区，收到 {as_of!r}")

    with conn.transaction():
        conn.execute("SELECT set_config('app.as_of', %s, true)", (as_of.isoformat(),))
        rows = conn.execute(
            "SELECT block_id, chunking_version, embedding_version"
            "  FROM asof.doc_block WHERE is_leaf ORDER BY block_id"
        ).fetchall()

    block_ids = tuple(int(r[0]) for r in rows)
    chunking_versions = tuple(sorted({str(r[1]) for r in rows if r[1] is not None}))
    embedding_versions = tuple(sorted({str(r[2]) for r in rows if r[2] is not None}))
    return EvalFreeze(
        as_of=as_of.isoformat(),
        block_ids=block_ids,
        chunking_versions=chunking_versions,
        embedding_versions=embedding_versions,
        created_at=datetime.now(UTC).isoformat(),
    )


def save_freeze(freeze: EvalFreeze, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(freeze), ensure_ascii=False, indent=2), encoding="utf-8")


def load_freeze(path: Path) -> EvalFreeze:
    data = json.loads(path.read_text(encoding="utf-8"))
    return EvalFreeze(
        as_of=data["as_of"],
        block_ids=tuple(data["block_ids"]),
        chunking_versions=tuple(data["chunking_versions"]),
        embedding_versions=tuple(data["embedding_versions"]),
        created_at=data["created_at"],
    )


def diff_against_current_corpus(conn: psycopg.Connection, freeze: EvalFreeze) -> list[str]:
    """重放同一个 as_of，跟快照存的块集合比对——不一致就说明"语料变了"，
    而不是"检索改好了/改坏了"，分数变化不该被误读。返回不一致描述列表；
    空列表表示语料库仍与冻结快照一致（H5：冻结快照后重跑三次，结果应
    完全一致）。
    """
    replay = create_freeze(conn, freeze.as_of_dt())
    diffs: list[str] = []
    if replay.block_ids != freeze.block_ids:
        added = set(replay.block_ids) - set(freeze.block_ids)
        removed = set(freeze.block_ids) - set(replay.block_ids)
        if added:
            preview = sorted(added)[:20]
            diffs.append(f"新增了 {len(added)} 个块（冻结快照之后才可见）: {preview}")
        if removed:
            preview = sorted(removed)[:20]
            diffs.append(f"少了 {len(removed)} 个块（冻结快照里有，现在查不到了）: {preview}")
    if replay.chunking_versions != freeze.chunking_versions:
        diffs.append(
            f"chunking_version 变了: {freeze.chunking_versions} -> {replay.chunking_versions}"
        )
    if replay.embedding_versions != freeze.embedding_versions:
        diffs.append(
            f"embedding_version 变了: {freeze.embedding_versions} -> {replay.embedding_versions}"
        )
    return diffs


__all__ = (
    "EvalFreeze",
    "create_freeze",
    "diff_against_current_corpus",
    "load_freeze",
    "save_freeze",
)
