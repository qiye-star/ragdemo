"""公告目录写入器（公告双源接入计划 Task 3）：跨源去重、主源取代降级行、标题归一化。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from ragdemo.ingest.announcement_index import (
    IndexEntry,
    IndexOutcome,
    IndexWriter,
    known_at_for_day,
    normalize_title,
)
from ragdemo.ingest.writer import UnknownEntityRef
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
CST = timezone(timedelta(hours=8))


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.688041', '海光信息', 'listed', 'SSE', '算力', 'AI芯片', ARRAY['通用服务器CPU'],"
        "  '通用服务器CPU', '688041.SH')"
    )
    c.commit()
    yield c
    c.close()


def _writer(conn: psycopg.Connection, now: datetime = NOW) -> IndexWriter:
    return IndexWriter(conn, ingest_run_id="t", now=lambda: now)


def _entry(**kw: object) -> IndexEntry:
    base = {
        "entity_ref": "688041.SH",
        "source": "cninfo",
        "source_ref": "1",
        "title": "2026年半年度报告",
        "publish_date": date(2026, 8, 14),
        "known_at": known_at_for_day(date(2026, 8, 14)),
        "has_full_text": True,
    }
    base.update(kw)
    return IndexEntry(**base)  # type: ignore[arg-type]


# --- known_at_for_day -------------------------------------------------------------


def test_known_at_for_day_is_end_of_day_beijing() -> None:
    got = known_at_for_day(date(2026, 8, 14))
    assert got == datetime(2026, 8, 14, 23, 59, 59, 999999, tzinfo=CST)


# --- normalize_title ---------------------------------------------------------------


def test_normalize_title_strips_name_prefix_and_width() -> None:
    got = normalize_title("海光信息：２０２６年半年度报告 ", ["海光信息", "海光"])
    assert got == "2026年半年度报告"


def test_normalize_title_without_a_matching_prefix_is_unchanged_but_trimmed() -> None:
    assert normalize_title("  某份没有前缀的公告  ", ["海光信息"]) == "某份没有前缀的公告"


# --- IndexWriter ---------------------------------------------------------------


@pytest.mark.db
def test_first_write_is_inserted(conn: psycopg.Connection) -> None:
    outcome, index_id = _writer(conn).write(_entry())
    conn.commit()
    assert outcome == IndexOutcome.INSERTED
    row = conn.execute(
        "SELECT source, source_ref, known_at FROM core.announcement_index WHERE index_id = %s",
        (index_id,),
    ).fetchone()
    assert row == ("cninfo", "1", known_at_for_day(date(2026, 8, 14)))


@pytest.mark.db
def test_same_source_ref_twice_is_skipped(conn: psycopg.Connection) -> None:
    writer = _writer(conn)
    writer.write(_entry())
    conn.commit()
    outcome, _ = writer.write(_entry())
    conn.commit()
    assert outcome == IndexOutcome.SKIPPED
    count = conn.execute("SELECT count(*) FROM core.announcement_index").fetchone()
    assert count == (1,)


@pytest.mark.db
def test_known_at_is_passed_through_not_recomputed(conn: psycopg.Connection) -> None:
    """写入器不做"当日 23:59"这类换算——那是各适配器按自己的时间精度算的事
    （EDGAR 秒级、cninfo/披露易日/分钟级各自不同）。"""
    exact = datetime(2026, 8, 14, 18, 32, 7, tzinfo=UTC)
    writer = _writer(conn)
    _, index_id = writer.write(_entry(source="edgar", source_ref="acc-1", known_at=exact))
    conn.commit()
    row = conn.execute(
        "SELECT known_at FROM core.announcement_index WHERE index_id = %s", (index_id,)
    ).fetchone()
    assert row == (exact,)


@pytest.mark.db
def test_cninfo_supersedes_degraded_row_for_the_same_announcement(
    conn: psycopg.Connection,
) -> None:
    writer = _writer(conn)
    wind_outcome, wind_id = writer.write(
        _entry(
            source="wind",
            source_ref="wind-1",
            title="海光信息:2026年半年度报告",
            has_full_text=False,
        )
    )
    conn.commit()
    assert wind_outcome == IndexOutcome.INSERTED

    later = NOW + timedelta(hours=1)
    writer_later = _writer(conn, now=later)
    cninfo_outcome, cninfo_id = writer_later.write(
        _entry(source="cninfo", source_ref="cninfo-1", title="2026年半年度报告")
    )
    conn.commit()
    assert cninfo_outcome == IndexOutcome.SUPERSEDED_DEGRADED

    row = conn.execute(
        "SELECT superseded_at, source_note FROM core.announcement_index WHERE index_id = %s",
        (wind_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == later and "cninfo-1" in str(row[1])

    live_sources = {
        r[0]
        for r in conn.execute(
            "SELECT source FROM core.announcement_index WHERE superseded_at IS NULL"
        ).fetchall()
    }
    assert live_sources == {"cninfo"}
    assert cninfo_id != wind_id


@pytest.mark.db
def test_degraded_row_is_shadowed_when_primary_already_has_it(conn: psycopg.Connection) -> None:
    writer = _writer(conn)
    writer.write(_entry(source="cninfo", source_ref="cninfo-1", title="2026年半年度报告"))
    conn.commit()

    outcome, index_id = writer.write(
        _entry(
            source="wind",
            source_ref="wind-1",
            title="海光信息:2026年半年度报告",
            has_full_text=False,
        )
    )
    conn.commit()
    assert outcome == IndexOutcome.SHADOWED
    row = conn.execute(
        "SELECT superseded_at FROM core.announcement_index WHERE index_id = %s", (index_id,)
    ).fetchone()
    assert row == (None,), "被压制的降级行本身没有写活行；这里的 index_id 是它自己那一行"


@pytest.mark.db
def test_different_titles_same_day_both_live(conn: psycopg.Connection) -> None:
    writer = _writer(conn)
    writer.write(_entry(source="cninfo", source_ref="a", title="2026年半年度报告"))
    writer.write(_entry(source="cninfo", source_ref="b", title="关于召开股东会的通知"))
    conn.commit()
    count = conn.execute(
        "SELECT count(*) FROM core.announcement_index WHERE superseded_at IS NULL"
    ).fetchone()
    assert count == (2,)


@pytest.mark.db
def test_unknown_entity_ref_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(UnknownEntityRef):
        _writer(conn).write(_entry(entity_ref="999999.SH"))


@pytest.mark.db
def test_link_document_sets_doc_id(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id)"
        " OVERRIDING SYSTEM VALUE VALUES (1, 'CN.688041', 'announcement', '2026年半年度报告',"
        " '2026-08-14 23:59:59.999999+08', 'cninfo', 'h1', 1, '2026-08-14',"
        " '2026-08-14 23:59:59.999999+08', 't')"
    )
    writer = _writer(conn)
    _, index_id = writer.write(_entry())
    conn.commit()
    writer.link_document(index_id, 1)
    conn.commit()
    row = conn.execute(
        "SELECT doc_id FROM core.announcement_index WHERE index_id = %s", (index_id,)
    ).fetchone()
    assert row == (1,)


@pytest.mark.db
def test_snapshot_id_is_stored(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO core.provider_snapshot (snapshot_id, provider, endpoint, params, response,"
        " http_status, fetched_at, ingest_run_id)"
        " OVERRIDING SYSTEM VALUE VALUES (1, 'cninfo', '/x', '{}', '{}', 200, now(), 't')"
    )
    _, index_id = _writer(conn).write(_entry(snapshot_id=1))
    conn.commit()
    row = conn.execute(
        "SELECT snapshot_id FROM core.announcement_index WHERE index_id = %s", (index_id,)
    ).fetchone()
    assert row == (1,)


@pytest.mark.db
def test_linked_doc_id_is_none_until_a_document_is_linked(conn: psycopg.Connection) -> None:
    writer = _writer(conn)
    _, index_id = writer.write(_entry())
    conn.commit()
    assert writer.linked_doc_id(index_id) is None


@pytest.mark.db
def test_full_text_arriving_later_supersedes_the_same_source_row_without_it(
    conn: psycopg.Connection,
) -> None:
    """先只建目录（has_full_text=False，如实体当时不在下载范围内），之后同一来源同一
    公告带着全文再来：不能因为 (source, source_ref) 已存在就 SKIPPED——那样目录行永远
    写着"没有全文"，而 core.document 里其实已经有了。旧行被取代、新行带全文。"""
    writer = _writer(conn)
    _, first = writer.write(_entry(has_full_text=False))
    conn.commit()
    outcome, second = writer.write(_entry(has_full_text=True))
    conn.commit()
    assert outcome is IndexOutcome.SUPERSEDED_DEGRADED
    assert second != first
    live = conn.execute(
        "SELECT index_id, has_full_text FROM core.announcement_index"
        " WHERE source = 'cninfo' AND source_ref = '1' AND superseded_at IS NULL"
    ).fetchall()
    assert live == [(second, True)]


@pytest.mark.db
def test_same_source_row_with_full_text_is_never_downgraded_by_a_later_listing_only_write(
    conn: psycopg.Connection,
) -> None:
    writer = _writer(conn)
    _, first = writer.write(_entry(has_full_text=True))
    conn.commit()
    outcome, again = writer.write(_entry(has_full_text=False))
    assert outcome is IndexOutcome.SKIPPED and again == first


@pytest.mark.db
def test_same_day_full_text_supplement_does_not_violate_the_time_order(
    conn: psycopg.Connection,
) -> None:
    """回归（2026-09-29 补下载海光公告时实测）：当天发布的公告，日精度 known_at 是当日
    23:59:59.999999，此刻 `now` 更早——补全文取代旧行不能把 superseded_at 写成 `now`
    （违反 superseded_at > known_at）。"""
    today = date(2026, 9, 28)
    morning = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)  # 早于当日日精度 known_at
    listing = _entry(
        source_ref="9", publish_date=today, known_at=known_at_for_day(today), has_full_text=False
    )
    _writer(conn, morning).write(listing)
    outcome, _ = _writer(conn, morning).write(replace_full_text(listing))
    conn.commit()
    assert outcome is IndexOutcome.SUPERSEDED_DEGRADED
    old, new = conn.execute(
        "SELECT superseded_at, known_at FROM core.announcement_index ORDER BY index_id"
    ).fetchall()
    assert old[0] == old[1] + timedelta(microseconds=1)  # 新旧同一 known_at，旧行紧随其后被取代
    assert new[0] is None


def replace_full_text(entry: IndexEntry) -> IndexEntry:
    from dataclasses import replace

    return replace(entry, has_full_text=True)


@pytest.mark.db
def test_only_one_row_is_visible_at_any_as_of_after_a_same_source_supplement(
    conn: psycopg.Connection,
) -> None:
    """同来源补全文后，任何 as_of 都只看到一行（不是补全文那一刻之前两行并存）。"""
    from ragdemo_core.db.session import as_of_session

    day = date(2026, 8, 14)
    known = known_at_for_day(day)
    listing = _entry(source_ref="10", publish_date=day, known_at=known, has_full_text=False)
    _writer(conn, NOW).write(listing)
    _writer(conn, NOW + timedelta(days=30)).write(replace_full_text(listing))
    conn.commit()
    for as_of in (known + timedelta(hours=1), NOW + timedelta(days=10), NOW + timedelta(days=60)):
        with as_of_session(conn, as_of):
            rows = conn.execute("SELECT has_full_text FROM asof.announcement_index").fetchall()
        assert rows == [(True,)], as_of


@pytest.mark.db
def test_primary_arriving_before_the_degraded_rows_known_at_supersedes_without_violation(
    conn: psycopg.Connection,
) -> None:
    today = date(2026, 9, 28)
    morning = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    degraded = _entry(
        source="ifind",
        source_ref="i1",
        publish_date=today,
        known_at=known_at_for_day(today),
        has_full_text=False,
    )
    _writer(conn, morning).write(degraded)
    outcome, _ = _writer(conn, morning).write(
        _entry(
            source="cninfo",
            source_ref="c1",
            publish_date=today,
            known_at=known_at_for_day(today),
            has_full_text=True,
        )
    )
    conn.commit()
    assert outcome is IndexOutcome.SUPERSEDED_DEGRADED
