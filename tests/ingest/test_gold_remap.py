"""评测 gold 块号重映射：重解析取代旧块后，gold 指向的旧块
换成新文档里内容相同（或高度重合）的现行块。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.ingest.gold_remap import (
    BlockText,
    case_remap,
    coverage,
    load_case_remaps,
    normalize_text,
    remap_block,
    remap_sql,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def _b(block_id: int, content: str, *, page: int | None = 1, ordinal: int = 0) -> BlockText:
    return BlockText(block_id=block_id, doc_id=9, page=page, ordinal=ordinal, content=content)


def test_normalize_drops_all_whitespace() -> None:
    assert normalize_text(" 营业 收入\n 100 \t万元 ") == "营业收入100万元"


def test_coverage_is_the_share_of_the_old_text_found_in_the_new_block() -> None:
    assert coverage("abc", "xxabcxx") == 1.0
    assert coverage("abcdefghij", "abcdefghi") == pytest.approx(0.9)
    assert coverage("abc", "abd") == 0.0
    assert coverage("", "abc") == 0.0


def test_exact_match_wins_and_prefers_the_same_page_then_the_nearest_ordinal() -> None:
    old = _b(1, "同一段 文字", page=2, ordinal=5)
    cands = [
        _b(10, "同一段文字", page=3, ordinal=5),
        _b(11, "同一段文字", page=2, ordinal=9),
        _b(12, "同一段文字", page=2, ordinal=6),
    ]
    r = remap_block(old, cands)
    assert (r.kind, r.new_block_id, r.coverage) == ("exact", 12, 1.0)


def test_overlap_needs_containment_the_same_page_and_ninety_percent() -> None:
    old = _b(1, "第一句。第二句。", page=4)
    merged = _b(20, "前文。第一句。第二句。后文。", page=4)
    assert remap_block(old, [merged]).kind == "overlap"
    assert remap_block(old, [merged]).new_block_id == 20
    # 同样的包含关系，但页码不同：不认
    assert remap_block(old, [_b(21, "前文。第一句。第二句。", page=3)]).kind == "lost"
    # 旧块被拆成两半：每一半只覆盖约一半旧文本，不够 0.9
    halves = [_b(22, "第一句。", page=4), _b(23, "第二句。", page=4)]
    assert remap_block(old, halves).kind == "lost"


def test_overlap_compares_bodies_without_the_section_path_prefix() -> None:
    """切块器在每个叶子块前加「章节路径\\n」：旧块正文若不在新块开头，带前缀比较永远不是包含关系。"""
    s = "年报摘要 > 二、基本情况"
    old = BlockText(1, 9, 3, 5, f"{s}\n公司采用追溯调整法。", section_path=s)
    new = BlockText(2, 9, 3, 1, f"{s}\n财政部印发指南。公司采用追溯调整法。", section_path=s)
    r = remap_block(old, [new])
    assert (r.kind, r.new_block_id, r.coverage) == ("overlap", 2, 1.0)


def test_overlap_prefers_the_highest_coverage_then_the_tightest_block() -> None:
    old = _b(1, "abcdefghij", page=1)
    cands = [
        _b(30, "XXXXXXXXXXabcdefghijYYYYYYYYYY", page=1),
        _b(31, "Xabcdefghij", page=1),
        _b(32, "abcdefghi", page=1),  # 被旧块包含，覆盖 0.9
    ]
    assert remap_block(old, cands).new_block_id == 31


def test_case_remap_replaces_ids_dedupes_and_keeps_lost_ones() -> None:
    lookup = {
        1: remap_block(_b(1, "a"), [_b(100, "a")]),
        2: remap_block(_b(2, "b"), [_b(100, "zbz")]),  # 合并进同一个新块
        3: remap_block(_b(3, "c"), []),
    }
    c = case_remap(7, "auto:x", (1, 2, 3), lookup.__getitem__)
    assert c.new_ids == (100, 3)
    assert c.kind == "lost" and c.changed


def _seed(conn: psycopg.Connection) -> tuple[int, int, list[int], list[int]]:
    t = datetime(2026, 4, 8, tzinfo=UTC)

    def doc(h: str, sup: int | None) -> int:
        row = conn.execute(
            "INSERT INTO core.document (doc_type, title, publish_at, language, source,"
            " content_hash, version_group_id, supersedes_doc_id, parse_engine, valid_from,"
            " known_at, ingest_run_id, can_show_raw, time_precision)"
            " VALUES ('announcement','t',%s,'zh','mock-announcements',%s,0,%s,'textin:x',"
            " %s,%s,'r',true,'second') RETURNING doc_id",
            (t, h, sup, t.date(), t),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def block(doc_id: int, ordinal: int, content: str, page: int) -> int:
        row = conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, ordinal, page, content, is_leaf,"
            " doc_type, publish_at, valid_from, known_at, source, ingest_run_id)"
            " VALUES (%s,'paragraph',%s,%s,%s,true,'announcement',%s,%s,%s,"
            " 'mock-announcements','r') RETURNING block_id",
            (doc_id, ordinal, page, content, t, t.date(), t),
        ).fetchone()
        assert row is not None
        return int(row[0])

    old = doc("h1", None)
    old_blocks = [block(old, 0, "甲段", 1), block(old, 1, "乙段", 1), block(old, 2, "丙段", 2)]
    conn.execute("UPDATE core.document SET superseded_at = now() WHERE doc_id = %s", (old,))
    conn.execute("UPDATE core.doc_block SET superseded_at = now() WHERE doc_id = %s", (old,))
    new = doc("h1", old)
    new_blocks = [block(new, 0, "甲段", 1), block(new, 1, "乙段和更多", 1), block(new, 2, "x", 2)]
    conn.commit()
    return old, new, old_blocks, new_blocks


@pytest.mark.db
def test_load_and_apply_remap_on_a_database(temp_db: str) -> None:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    _old, _new, ob, nb = _seed(conn)
    conn.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, author)"
        " VALUES ('q1', now(), %s, 'auto:t'), ('q2', now(), %s, 'blindmodel:t'),"
        " ('q3', now(), %s, 'human:t')",
        ([ob[0], ob[1]], [ob[2]], [nb[0]]),
    )
    conn.commit()

    remaps = load_case_remaps(conn)
    by_q = {r.q_id: r for r in remaps}
    assert [b.kind for b in by_q[1].blocks] == ["exact", "overlap"]
    assert by_q[1].new_ids == (nb[0], nb[1])
    assert by_q[2].kind == "lost" and by_q[2].new_ids == (ob[2],)
    assert by_q[3].kind == "live" and not by_q[3].changed

    sql = remap_sql(remaps)
    assert "UPDATE evals.eval_retrieval" in sql and "RAISE EXCEPTION" in sql
    conn.execute(sql)
    conn.commit()
    got: dict[int, list[int]] = dict(
        conn.execute("SELECT q_id, gold_block_ids FROM evals.eval_retrieval").fetchall()
    )
    assert got[1] == [nb[0], nb[1]]
    assert got[2] == [ob[2]]  # 丢失的保留原样，交给人工
    # 再跑一遍：gold 已经变了，守卫条件不再匹配 -> 整体报错回滚，不会二次改写
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute(sql)
    conn.rollback()
