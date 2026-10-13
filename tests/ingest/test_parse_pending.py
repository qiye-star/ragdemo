"""解析已存原文、尚未切块的文档（公告双源接入计划 Task 10）。

这是"先只对 2 家试点解析首批档，以后扩大范围"的落地入口——不重新抓取任何东西，
只对 core.document 里已经有 raw_ref、却还没有块的现行行重新走一遍解析。
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from pypdf import PdfWriter

from ragdemo.ingest.parse_pending import ParsePendingReport, run_parse_pending, tier_for_document
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.parse.textin import MockDocumentParser, PageBudget, ParseResult
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def _pdf_bytes(pages: int, *, marker: str = "") -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    # document_dedup_uk 是 (source, content_hash) 上的唯一索引；同页数的空白 PDF
    # 字节完全相同，不同测试文档必须靠这个 marker 撑开 content_hash，否则第二份
    # 插入会撞上第一份的去重约束。
    writer.add_metadata({"/Marker": marker})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type, market,"
        " l1_layer, l2_segment, l3_node, primary_node, tushare_code, edgar_cik) VALUES"
        " ('CN.688041', '海光信息技术股份有限公司', '海光信息', 'listed', 'SSE', '算力',"
        "  'AI芯片', ARRAY['云端训练芯片'], '云端训练芯片', '688041.SH', NULL),"
        " ('US.NVDA', 'NVIDIA Corporation', 'NVIDIA', 'listed', 'NASDAQ', '算力',"
        "  'AI芯片', ARRAY['云端训练芯片'], '云端训练芯片', NULL, '1045810')"
    )
    c.commit()
    yield c
    c.close()


def _insert_pending_document(
    conn: psycopg.Connection,
    blob: LocalBlobStore,
    *,
    entity_id: str,
    source: str,
    doc_type: str,
    title: str,
    raw_bytes: bytes,
    source_ref: str,
    publish_at: datetime = datetime(2026, 8, 1, tzinfo=UTC),
) -> int:
    """直接插入一行"现行、无块、有 raw_ref"的 core.document——模拟 Task 9 只请求
    ParseTier.FIRST 档解析后，剩下的那批文档（有全文、没入块）。不经过任何一个
    来源适配器：parse_pending 要处理的正是"已经在库里"的状态，不关心它怎么来的。
    """
    raw_ref = f"{source}/{source_ref}.pdf"
    blob.put(raw_ref, raw_bytes)
    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    row = conn.execute(
        "INSERT INTO core.document (entity_id, doc_type, title, publish_at, language,"
        " source, source_url, raw_ref, content_hash, version_group_id, valid_from,"
        " known_at, source_ref, ingest_run_id) VALUES"
        " (%s,%s,%s,%s,'zh',%s,%s,%s,%s,0,%s,%s,%s,'t') RETURNING doc_id",
        (
            entity_id,
            doc_type,
            title,
            publish_at,
            source,
            f"https://example.invalid/{source_ref}",
            raw_ref,
            content_hash,
            publish_at.date(),
            publish_at,
            source_ref,
        ),
    ).fetchone()
    assert row is not None
    doc_id = int(row[0])
    conn.execute(
        "UPDATE core.document SET version_group_id = %s WHERE doc_id = %s", (doc_id, doc_id)
    )
    conn.commit()
    return doc_id


def _run(
    conn: psycopg.Connection,
    tmp_path: Path,
    *,
    entity_ref: str | None = None,
    tiers: frozenset[ParseTier] = frozenset({ParseTier.FIRST}),
    budget: PageBudget | None = None,
) -> ParsePendingReport:
    return run_parse_pending(
        conn,
        parser=MockDocumentParser(),
        blob=LocalBlobStore(tmp_path),
        entity_ref=entity_ref,
        tiers=tiers,
        budget=budget or PageBudget(remaining=1000),
        ingest_run_id="t",
        dry_run=False,
    )


def test_tier_for_document_uses_doc_type_for_edgar_and_title_for_the_rest() -> None:
    assert tier_for_document("edgar", "10-Q", "10-Q 0001045810-24-000316") == ParseTier.FIRST
    assert tier_for_document("edgar", "10-K", "10-K 0001045810-24-000001") == ParseTier.SECOND
    assert tier_for_document("cninfo", "announcement", "海光信息:2025年第三季度报告") == (
        ParseTier.FIRST
    )
    assert tier_for_document("cninfo", "announcement", "海光信息:2025年年度报告") == (
        ParseTier.SECOND
    )


@pytest.mark.db
def test_reparse_keeps_the_original_known_at(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = _insert_pending_document(
        conn,
        blob,
        entity_id="CN.688041",
        source="cninfo",
        doc_type="announcement",
        title="海光信息:2025年第三季度报告",
        raw_bytes=_pdf_bytes(3),
        source_ref="1",
    )
    original_known_at = conn.execute(
        "SELECT known_at FROM core.document WHERE doc_id = %s", (doc_id,)
    ).fetchone()
    assert original_known_at is not None

    report = _run(conn, tmp_path, entity_ref="688041.SH")
    assert report.reparsed == 1

    row = conn.execute(
        "SELECT known_at, superseded_at FROM core.document WHERE doc_id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    new_known_at, superseded_at = row
    assert superseded_at is not None, "旧行应被标记为已取代"
    assert new_known_at == original_known_at[0], "重解析必须沿用原文档的 known_at"


@pytest.mark.db
def test_dry_run_reports_real_page_counts_and_cost(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    _insert_pending_document(
        conn,
        blob,
        entity_id="CN.688041",
        source="cninfo",
        doc_type="announcement",
        title="海光信息:2025年第三季度报告",
        raw_bytes=_pdf_bytes(7),
        source_ref="2",
    )
    report = run_parse_pending(
        conn,
        parser=MockDocumentParser(),
        blob=blob,
        entity_ref="688041.SH",
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        ingest_run_id="t",
        dry_run=True,
    )
    assert report.selected == 1
    assert report.estimated_pages[ParseTier.FIRST] == 7, "有真实 PDF 时用真实页数，不用经验估算"
    # dry run 绝不碰数据库写入路径。
    row = conn.execute("SELECT superseded_at FROM core.document WHERE source = 'cninfo'").fetchone()
    assert row == (None,)


@pytest.mark.db
def test_only_requested_tier_is_parsed(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    first_tier_id = _insert_pending_document(
        conn,
        blob,
        entity_id="CN.688041",
        source="cninfo",
        doc_type="announcement",
        title="海光信息:2025年第三季度报告",
        raw_bytes=_pdf_bytes(1, marker="3"),
        source_ref="3",
    )
    second_tier_id = _insert_pending_document(
        conn,
        blob,
        entity_id="CN.688041",
        source="cninfo",
        doc_type="announcement",
        title="海光信息:2025年年度报告",
        raw_bytes=_pdf_bytes(1, marker="4"),
        source_ref="4",
    )

    report = _run(conn, tmp_path, entity_ref="688041.SH", tiers=frozenset({ParseTier.SECOND}))
    assert report.selected == 1 and report.reparsed == 1

    first_tier_superseded = conn.execute(
        "SELECT superseded_at FROM core.document WHERE doc_id = %s", (first_tier_id,)
    ).fetchone()
    assert first_tier_superseded == (None,), "只请求 SECOND 档，FIRST 档文档不该被碰"

    second_tier_superseded = conn.execute(
        "SELECT superseded_at FROM core.document WHERE doc_id = %s", (second_tier_id,)
    ).fetchone()
    assert second_tier_superseded != (None,)


@pytest.mark.db
def test_budget_stops_between_documents(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    for i in range(3):
        _insert_pending_document(
            conn,
            blob,
            entity_id="CN.688041",
            source="cninfo",
            doc_type="announcement",
            title="海光信息:2025年第三季度报告",
            raw_bytes=_pdf_bytes(2, marker=f"budget-{i}"),
            source_ref=f"budget-{i}",
        )

    # MockDocumentParser 报的 page_count 固定是 2（见 parse/textin.py），预算只够
    # 一份的量——第二份开始就该被跳过而不是报错。
    report = _run(conn, tmp_path, entity_ref="688041.SH", budget=PageBudget(remaining=2))
    assert report.selected == 3
    assert report.reparsed == 1
    assert report.skipped_budget == 2


@pytest.mark.db
def test_edgar_document_without_blocks_is_picked_up(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = _insert_pending_document(
        conn,
        blob,
        entity_id="US.NVDA",
        source="edgar",
        doc_type="10-Q",
        title="10-Q 0001045810-24-000316",
        raw_bytes=_pdf_bytes(2),
        source_ref="0001045810-24-000316",
    )

    report = _run(conn, tmp_path, entity_ref="1045810")
    assert report.selected == 1 and report.reparsed == 1

    superseded_at = conn.execute(
        "SELECT superseded_at FROM core.document WHERE doc_id = %s", (doc_id,)
    ).fetchone()
    assert superseded_at != (None,)


@pytest.mark.db
def test_doc_ids_restrict_the_selection(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    keep = _insert_pending_document(
        conn,
        blob,
        entity_id="CN.688041",
        source="cninfo",
        doc_type="announcement",
        title="海光信息:关于召开股东会的通知",
        raw_bytes=_pdf_bytes(1, marker="5"),
        source_ref="5",
    )
    _insert_pending_document(
        conn,
        blob,
        entity_id="CN.688041",
        source="cninfo",
        doc_type="announcement",
        title="海光信息:关于董事会决议的公告",
        raw_bytes=_pdf_bytes(1, marker="6"),
        source_ref="6",
    )
    report = run_parse_pending(
        conn,
        parser=MockDocumentParser(),
        blob=blob,
        entity_ref=None,
        tiers=frozenset({ParseTier.NONE}),
        budget=PageBudget(remaining=100),
        ingest_run_id="t",
        dry_run=True,
        doc_ids=frozenset({keep}),
    )
    assert report.selected == 1


# --- EDGAR htm 派生 PDF（EDGAR 8-K 转 PDF 方案 §4.5 / §4.6-3）-----------------

HTM_BODY = b"<html><body><p>nvidia 8-K</p></body></html>"
HTM_REF = "edgar/0001045810/0001045810-26-000078/nvda-20260902.htm"


class _CountingParser(MockDocumentParser):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        self.calls += 1
        self.last_bytes = file_bytes
        return super().parse(file_bytes, owner_user=owner_user)


def _htm_doc(conn: psycopg.Connection, blob: LocalBlobStore) -> int:
    from tests.ingest.test_render_html import insert_htm_doc

    return insert_htm_doc(conn, blob, entity_id="US.NVDA", raw_ref=HTM_REF, body=HTM_BODY)


def _render_derived(
    conn: psycopg.Connection, blob: LocalBlobStore, doc_id: int, pages: int
) -> None:
    from ragdemo.adapters.mock.html_render import MockHtmlRenderer
    from ragdemo.ingest.render_html import render_html_document

    render_html_document(
        conn,
        doc_id=doc_id,
        renderer=MockHtmlRenderer(pages=pages),
        blob=blob,
        ingest_run_id="t",
        dry_run=False,
    )


def _dry(
    conn: psycopg.Connection, blob: LocalBlobStore, parser: MockDocumentParser
) -> ParsePendingReport:
    return run_parse_pending(
        conn,
        parser=parser,
        blob=blob,
        entity_ref="1045810",
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        ingest_run_id="t",
        dry_run=True,
    )


@pytest.mark.db
def test_dry_run_uses_derived_pdf_page_count(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = _htm_doc(conn, blob)
    _render_derived(conn, blob, doc_id, pages=6)
    report = _dry(conn, blob, MockDocumentParser())
    assert report.selected == 1
    assert report.estimated_pages[ParseTier.FIRST] == 6, "真实页数 6，不是 12 的估算"


@pytest.mark.db
def test_htm_without_derived_pdf_fails_loudly_and_never_parses(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    from ragdemo.ingest.parse_pending import DerivedPdfMissing

    blob = LocalBlobStore(tmp_path)
    _htm_doc(conn, blob)
    parser = _CountingParser()
    with pytest.raises(DerivedPdfMissing, match="render-html"):
        _dry(conn, blob, parser)
    with pytest.raises(DerivedPdfMissing, match="render-html"):
        run_parse_pending(
            conn,
            parser=parser,
            blob=blob,
            entity_ref="1045810",
            tiers=frozenset({ParseTier.FIRST}),
            budget=PageBudget(remaining=1000),
            ingest_run_id="t",
            dry_run=False,
        )
    assert parser.calls == 0


@pytest.mark.db
def test_reparse_from_derived_pdf_keeps_html_hash_and_marks_derivation(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    import json

    blob = LocalBlobStore(tmp_path)
    doc_id = _htm_doc(conn, blob)
    _render_derived(conn, blob, doc_id, pages=6)
    old = conn.execute(
        "SELECT known_at, content_hash FROM core.document WHERE doc_id = %s", (doc_id,)
    ).fetchone()
    assert old is not None
    render_snap = conn.execute(
        "SELECT snapshot_id FROM core.provider_snapshot WHERE provider = 'local-html-render'"
    ).fetchone()
    assert render_snap is not None

    parser = _CountingParser()
    report = run_parse_pending(
        conn,
        parser=parser,
        blob=blob,
        entity_ref="1045810",
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        ingest_run_id="t",
        dry_run=False,
    )
    assert report.reparsed == 1 and parser.calls == 1
    assert parser.last_bytes.startswith(b"%PDF"), "送去解析的必须是 PDF，不是 htm"

    row = conn.execute(
        "SELECT raw_ref, content_hash, known_at, parse_warnings, snapshot_id, entity_id,"
        "       source_url FROM core.document"
        " WHERE supersedes_doc_id = %s AND superseded_at IS NULL",
        (doc_id,),
    ).fetchone()
    assert row is not None
    raw_ref, content_hash, known_at, warnings, snapshot_id, entity_id, source_url = row
    assert raw_ref == "edgar/0001045810/0001045810-26-000078/nvda-20260902.rendered.pdf"
    assert content_hash == old[1] == hashlib.sha256(HTM_BODY).hexdigest()
    assert known_at == old[0]
    assert f"derived_from_html:{old[1][:12]}" in json.loads(json.dumps(warnings))
    assert snapshot_id == render_snap[0]
    assert entity_id == "US.NVDA"
    assert source_url == "https://sec.invalid/x.htm"
    # 旧 htm 原文仍在
    assert blob.get(HTM_REF) == HTM_BODY


@pytest.mark.db
def test_rerun_after_reparse_does_not_select_again(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = _htm_doc(conn, blob)
    _render_derived(conn, blob, doc_id, pages=2)
    _run(conn, tmp_path, entity_ref="1045810")
    again = _run(conn, tmp_path, entity_ref="1045810")
    assert again.selected == 0
    n = conn.execute(
        "SELECT count(*) FROM core.document WHERE source = 'edgar' AND superseded_at IS NULL"
    ).fetchone()
    assert n == (1,)


@pytest.mark.db
def test_pdf_documents_keep_old_behaviour_without_marker(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = _insert_pending_document(
        conn,
        blob,
        entity_id="US.NVDA",
        source="edgar",
        doc_type="10-Q",
        title="10-Q x",
        raw_bytes=_pdf_bytes(2, marker="plain"),
        source_ref="plain-1",
    )
    _run(conn, tmp_path, entity_ref="1045810")
    row = conn.execute(
        "SELECT parse_warnings::text, raw_ref FROM core.document WHERE supersedes_doc_id = %s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    assert "derived_from_html" not in row[0]
    assert row[1].endswith(".pdf")
