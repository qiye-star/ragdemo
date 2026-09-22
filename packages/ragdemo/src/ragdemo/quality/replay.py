"""时点泄漏抽样重放（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 E）。

现有的 `check_point_in_time_leaks`（`ragdemo_core/db/invariants.py`）查的是
**写入时的错误**——扫全表找 `known_at` 早于 `period_end`/`publish_at` 的行。
这里查的是不同的东西：**写入之后被无痕修改**。做法是每日随机抽 200 条历史
`doc_block`，对每条取三个探测点——该块 `known_at` 之前 / `known_at` 本身 /
最近一次更正之后——经 `asof.doc_block` 视图重放查询，把结果集（含"这个
as_of 下根本查不到"这个状态本身）算成一个哈希，与上一次抽中同一
`(block_id, as_of)` 时记下的基线哈希比对。不一致就说明这条历史记录在两次
抽样之间被篡改过：可能是 `known_at` 被人为改早，让它在本该看不见的历史
`as_of` 下冒出来（这正是 CLAUDE.md §1.1 要防的泄漏本身），也可能是内容被
悄悄改写却没有走"新增一行、旧行标 superseded_at"这条唯一合法的更正路径。

抽样种子按 `partition_date` 派生，不是随机种子——同一天内重放必须抽中
同一批块，否则"今天不一致"这句话没法复查到底是哪些块出的问题。

三个探测点只在**第一次**抽中某个块时从它当时的 `known_at` 推算，随后
永久写进基线表；此后每次重放都从基线表读回同一批 `as_of`，绝不重新
从块当前的 `known_at` 现算（见 `_initial_probe_moments` 的 docstring）——
如果每次都现算，`known_at` 一旦被篡改，探测点会跟着篡改一起偏移，
比对的对象也跟着变，篡改反而永远逃过检测，这套机制就白做了。

已知局限：`_replay_hash` 不设置 `app.tenant`/`app.user`，只按公共可见性
（`owner_tenant`/`owner_user` 皆为 NULL）重放。私有材料的时点重放需要知道
"该以哪个用户身份重放"，这是一个尚未回答的设计问题，不在这次范围内——
当前抽样池也只包含公共测试数据，不影响本阶段的验收。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import psycopg

SAMPLE_SIZE = 200

# 哈希的哨兵值：这个 as_of 下这个块根本不在 asof.doc_block 里可见（正常情况，
# 不是异常）。真实的 sha256 十六进制摘要恒长 64，与这个字符串不可能相等，
# 不需要额外的"是否命中"标记位。
ABSENT = "ABSENT"


@dataclass(frozen=True)
class ReplayMismatch:
    block_id: int
    as_of: datetime
    baseline_hash: str
    replayed_hash: str


def _seed_for(partition_date: date) -> int:
    return int(hashlib.sha256(partition_date.isoformat().encode("utf-8")).hexdigest()[:8], 16)


def sample_block_ids(
    conn: psycopg.Connection, partition_date: date, *, size: int = SAMPLE_SIZE
) -> list[int]:
    """从全部历史 `doc_block` 里随机抽 `size` 个 `block_id`。

    种子由 `partition_date` 派生：同一天重跑必须抽中同一批块，可复现是
    抽样重放能"复查"的前提。样本池不做任何时点或权限过滤——连
    `superseded_at` 不为空的旧块也在池子里，它们恰恰是最需要被盯住的
    对象（篡改历史记录，改的通常就是这些"看起来已经作古"的行）。
    """
    all_ids = [int(r[0]) for r in conn.execute("SELECT block_id FROM core.doc_block").fetchall()]
    if not all_ids:
        return []
    rng = random.Random(_seed_for(partition_date))
    return sorted(rng.sample(all_ids, k=min(size, len(all_ids))))


def _existing_probe_moments(conn: psycopg.Connection, block_id: int) -> list[datetime]:
    """这个块之前被抽中过时，第一次记下的探测点——**必须复用**，不能重算。"""
    return [
        r[0]
        for r in conn.execute(
            "SELECT as_of FROM quality.asof_replay_baseline WHERE block_id = %s ORDER BY as_of",
            (block_id,),
        ).fetchall()
    ]


def _initial_probe_moments(conn: psycopg.Connection, block_id: int) -> list[datetime]:
    """第一次抽中这个块时，从它**当前**的 `known_at` 推出三个探测点：
    `known_at` 之前 / `known_at` 本身 / 最近一次更正之后。

    这三个具体的时间戳一旦算出来，就要被永久记进
    `quality.asof_replay_baseline`（`replay_and_check` 做这件事），此后
    任何一次重放都必须调用 `_existing_probe_moments` 复用它们，**不能**
    每次都重新调用这个函数从当前 `known_at` 现算——如果 `known_at` 本身
    被篡改了，每次都现算探测点，等于探测点跟着篡改一起变，比对的对象
    也跟着偏移，篡改反而永远逃过检测（这正是本函数存在两个变体的原因）。
    """
    row = conn.execute(
        "SELECT known_at, superseded_at FROM core.doc_block WHERE block_id = %s", (block_id,)
    ).fetchone()
    if row is None:
        return []
    known_at, superseded_at = row
    moments = [known_at - timedelta(seconds=1), known_at]
    if superseded_at is not None:
        moments.append(superseded_at)
    return moments


def _replay_hash(conn: psycopg.Connection, block_id: int, as_of: datetime) -> str:
    """在给定 `as_of` 下经 `asof.doc_block` 视图查这个块，把结果集哈希。

    `set_config` 的第三个参数（`is_local=true`）把 GUC 限定在当前事务内，
    不会泄漏到这个连接上后续的其他查询——每次探测都在自己的
    `with conn.transaction()` 块里设置、查询、结束，互不干扰。
    """
    with conn.transaction():
        conn.execute("SELECT set_config('app.as_of', %s, true)", (as_of.isoformat(),))
        row = conn.execute(
            "SELECT block_id, content, known_at, superseded_at"
            "  FROM asof.doc_block WHERE block_id = %s",
            (block_id,),
        ).fetchone()
    if row is None:
        return ABSENT
    canonical = "|".join(str(v) for v in row)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_and_check(
    conn: psycopg.Connection, partition_date: date, *, size: int = SAMPLE_SIZE
) -> list[ReplayMismatch]:
    """抽样重放的核心入口。返回不一致的列表；空列表表示全部一致。

    每个 `(block_id, as_of)` 组合首次遇到时把哈希记进
    `quality.asof_replay_baseline` 并提交，作为以后比对的基线；之后再次
    抽中同一组合，比对哈希，不一致就收进返回值——调用方（Dagster 资产或
    CLI）决定不一致要不要升级成 P0 告警并阻断下游。
    """
    mismatches: list[ReplayMismatch] = []
    for block_id in sample_block_ids(conn, partition_date, size=size):
        moments = _existing_probe_moments(conn, block_id) or _initial_probe_moments(conn, block_id)
        for as_of in moments:
            replayed = _replay_hash(conn, block_id, as_of)
            baseline_row = conn.execute(
                "SELECT result_hash FROM quality.asof_replay_baseline"
                " WHERE block_id = %s AND as_of = %s",
                (block_id, as_of),
            ).fetchone()
            if baseline_row is None:
                conn.execute(
                    "INSERT INTO quality.asof_replay_baseline (block_id, as_of, result_hash)"
                    " VALUES (%s,%s,%s)",
                    (block_id, as_of, replayed),
                )
                conn.commit()
                continue
            baseline_hash = str(baseline_row[0])
            if baseline_hash != replayed:
                mismatches.append(ReplayMismatch(block_id, as_of, baseline_hash, replayed))
    return mismatches
