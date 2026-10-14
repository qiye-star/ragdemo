"""旧文档缓存重解析（`ragdemo docs reparse-cached`）：只用已缓存的 TextIn JSON 与本地 PDF，
走新流程（TieredParser 整份 L3、无跳页、无 L0）重写块、逐页台账与表格网格；零外部调用。"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest
from click.testing import CliRunner
from pypdf import PdfWriter

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.cli import main
from ragdemo.ingest.assets_docs import build_chunks_and_descriptions
from ragdemo.ingest.documents import DocumentWriter, ParseArtifacts
from ragdemo.ingest.reparse_cached import (
    ReadOnlyBlob,
    ReplayMiss,
    ReplayTransport,
    carry_table_descriptions,
    run_reparse_cached,
)
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.textin import XPARSE_PARAMS, TextInParser, artifact_keys, param_fingerprint
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.invariants import check_schema_invariants
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
FIXTURE = Path("tests/fixtures/xparse/annual_report.json")


# --- 纯函数 ------------------------------------------------------------------


def test_replay_transport_raises_on_any_request() -> None:
    t = ReplayTransport()
    with pytest.raises(ReplayMiss), httpx.Client(transport=t) as c:
        c.post("http://replay.invalid/ai/service/v1/pdf_to_markdown", content=b"x")
    assert t.calls == 1


def test_read_only_blob_refuses_writes(tmp_path: Path) -> None:
    inner = LocalBlobStore(tmp_path)
    inner.put("a", b"1")
    ro = ReadOnlyBlob(inner)
    assert ro.exists("a") and ro.get("a") == b"1"
    with pytest.raises(PermissionError):
        ro.put("b", b"2")
    assert not inner.exists("b")


def _table(ordinal: int, content: str, page: int = 1) -> Chunk:
    return Chunk(ordinal, "table", "", content, page, None, True)


def test_table_descriptions_carry_over_only_for_identical_content() -> None:
    from ragdemo.ingest.reparse_cached import OldBlock

    def old(block_id: int, content: str, desc: str, model: str | None, page: int = 1) -> OldBlock:
        return OldBlock(
            block_id=block_id,
            ordinal=block_id,
            block_type="table",
            section_path="",
            page=page,
            content=content,
            content_desc=desc,
            is_leaf=True,
            has_embedding=False,
            desc_model=model,
            desc_prompt_version="p1" if model else None,
        )

    olds = [
        old(1, "|a|b|", "描述一", "deepseek-chat"),
        old(2, "|c|d|", "描述二", None),
        old(3, "|a|b|", "描述三（另一页）", "m", page=2),
    ]
    chunks = [_table(0, "|a|b|", page=2), _table(1, "|a|b|"), _table(2, "|x|y|")]
    desc, prov, missing = carry_table_descriptions(chunks, olds)
    # 同内容优先同页；一对一消耗
    assert desc == {0: "描述三（另一页）", 1: "描述一"}
    assert prov == {0: ("m", "p1"), 1: ("deepseek-chat", "p1")}
    assert missing == [2]


# --- 数据库 ------------------------------------------------------------------


def _two_page_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture()
def seeded(temp_db: str, tmp_path: Path) -> tuple[psycopg.Connection, LocalBlobStore, int]:
    """用旧路径（TextIn 缓存命中 + Mock 描述）写入一份有表格的现行文档。"""
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688041','海光信息技术股份有限公司','listed','算力','AI芯片',"
        " ARRAY['通用服务器CPU'],'通用服务器CPU','688041.SH')"
    )
    conn.commit()
    blob = LocalBlobStore(tmp_path / "blob")
    raw = _two_page_pdf()
    blob.put("raw/doc.pdf", raw)
    json_key, _ = artifact_keys(TextInParser.content_hash(raw), param_fingerprint(XPARSE_PARAMS))
    blob.put(json_key, FIXTURE.read_bytes())

    res = TextInParser("http://unused.invalid", blob, transport=ReplayTransport()).parse(raw)
    doc = NormalizedDocument(
        provider_doc_id="P-1",
        entity_ref="688041.SH",
        doc_type="announcement",
        title="海光信息关于事项的公告",
        period=None,
        publish_at=datetime(2026, 4, 8, 9, 30, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref="raw/doc.pdf",
        content_hash="h-doc",
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=res.page_count,
        blocks=res.blocks,
    )
    chunks, desc, violations = build_chunks_and_descriptions(
        doc, ChunkConfig(), MockTableDescriber()
    )
    assert not violations and any(c.block_type == "table" for c in chunks)
    w = DocumentWriter(conn, ingest_run_id="old-run", source="cninfo", blob=blob)
    first = w.write_document(
        doc,
        chunks,
        desc,
        artifacts=ParseArtifacts(res.engine_version, res.json_ref, res.md_ref),
        chunking_version=ChunkConfig().version,
        desc_provenance=("mock", "v1"),
    )
    # 旧表描述其实出自真实模型、旧叶子块都已嵌入：重解析必须把这些原样带过来
    conn.execute(
        "UPDATE core.doc_table SET desc_model = 'deepseek-chat', desc_prompt_version = 'tbl:v2'"
        " WHERE doc_id = %s",
        (first.doc_id,),
    )
    vec = "[" + ",".join(["0.5"] * 1024) + "]"
    conn.execute(
        "UPDATE core.doc_block SET embedding = %s::vector, embedding_version = 'BAAI/bge-m3'"
        " WHERE doc_id = %s AND is_leaf",
        (vec, first.doc_id),
    )
    conn.commit()
    return conn, blob, first.doc_id


def _blocks(conn: psycopg.Connection, doc_id: int) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT block_type::text, content, page, is_leaf, content_desc, parse_source,"
        " embedding IS NOT NULL, embedding_version, known_at, superseded_at"
        " FROM core.doc_block WHERE doc_id = %s ORDER BY ordinal",
        (doc_id,),
    ).fetchall()


def _state(conn: psycopg.Connection, doc_id: int) -> list[tuple[object, ...]]:
    """块号、块内容、描述、嵌入、网格——填元数据之外的一切都必须原样不动。"""
    return conn.execute(
        "SELECT b.block_id, b.ordinal, b.block_type::text, b.content, b.page, b.section_path,"
        " b.content_desc, b.embedding::text, b.embedding_version, b.known_at, b.superseded_at,"
        " b.ingest_run_id, t.table_id, t.desc_model, t.superseded_at"
        " FROM core.doc_block b LEFT JOIN core.doc_table t ON t.block_id = b.block_id"
        " WHERE b.doc_id = %s ORDER BY b.ordinal",
        (doc_id,),
    ).fetchall()


def _make_differ(conn: psycopg.Connection, doc_id: int) -> None:
    """把一个现行段落块改成与缓存不同的内容（模拟旧切块版本）。"""
    conn.execute(
        "UPDATE core.doc_block SET content = content || '（旧切块）' WHERE block_id = ("
        " SELECT min(block_id) FROM core.doc_block WHERE doc_id = %s AND is_leaf"
        " AND block_type = 'paragraph')",
        (doc_id,),
    )
    conn.commit()


@pytest.mark.db
def test_dry_run_writes_nothing(seeded: tuple[psycopg.Connection, LocalBlobStore, int]) -> None:
    conn, blob, doc_id = seeded
    before = _state(conn, doc_id)
    report = run_reparse_cached(conn, blob, doc_ids=None, write=False, ingest_run_id="t")
    assert [o.status for o in report.outcomes] == ["fill"]
    o = report.outcomes[0]
    assert o.old_blocks == o.new_blocks == o.identical_blocks and o.desc_missing == []
    assert o.state is not None
    assert _state(conn, doc_id) == before
    assert conn.execute(
        "SELECT count(*) FILTER (WHERE parse_source IS NOT NULL) FROM core.doc_block"
    ).fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM core.parse_page_route").fetchone() == (0,)


@pytest.mark.db
def test_identical_document_only_gets_metadata_filled_in_place(
    seeded: tuple[psycopg.Connection, LocalBlobStore, int],
) -> None:
    conn, blob, doc_id = seeded
    before = _state(conn, doc_id)
    report = run_reparse_cached(conn, blob, doc_ids=None, write=True, ingest_run_id="t")
    (o,) = report.outcomes
    assert o.status == "filled" and o.new_doc_id is None and report.replay_calls == 0
    # 块号、内容、描述、嵌入、时点列、网格全部原样；没有新文档、没有被取代的行
    assert _state(conn, doc_id) == before
    assert conn.execute("SELECT count(*) FROM core.document").fetchone() == (1,)
    assert {
        r[0]
        for r in conn.execute(
            "SELECT parse_source FROM core.doc_block WHERE doc_id = %s", (doc_id,)
        ).fetchall()
    } == {"textin"}
    (ref,) = conn.execute(
        "SELECT parse_json_ref FROM core.document WHERE doc_id = %s", (doc_id,)
    ).fetchone()  # type: ignore[misc]
    (state,) = conn.execute(
        "SELECT state FROM core.parse_doc_acceptance WHERE routes_ref = %s", (ref,)
    ).fetchone()  # type: ignore[misc]
    assert state == o.state
    assert conn.execute(
        "SELECT count(*) FROM core.parse_page_route WHERE routes_ref = %s", (ref,)
    ).fetchone() == (2,)
    assert check_schema_invariants(conn) == []


@pytest.mark.db
def test_differing_document_is_left_on_its_old_chunking_by_default(
    seeded: tuple[psycopg.Connection, LocalBlobStore, int],
) -> None:
    conn, blob, doc_id = seeded
    _make_differ(conn, doc_id)
    before = _state(conn, doc_id)
    report = run_reparse_cached(conn, blob, doc_ids=None, write=True, ingest_run_id="t")
    (o,) = report.outcomes
    assert o.status == "differs" and "as_of" in o.error
    assert o.identical_blocks == o.old_blocks - 1
    assert _state(conn, doc_id) == before
    assert conn.execute(
        "SELECT count(*) FILTER (WHERE parse_source IS NOT NULL) FROM core.doc_block"
    ).fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM core.parse_page_route").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM core.parse_doc_acceptance").fetchone() == (0,)


@pytest.mark.db
def test_replace_differing_supersedes_and_carries_descriptions_and_embeddings(
    seeded: tuple[psycopg.Connection, LocalBlobStore, int],
) -> None:
    conn, blob, old_id = seeded
    _make_differ(conn, old_id)
    before = _blocks(conn, old_id)
    report = run_reparse_cached(
        conn, blob, doc_ids=None, write=True, ingest_run_id="t", replace_differing=True
    )
    (o,) = report.outcomes
    assert o.status == "written" and o.new_doc_id is not None and report.replay_calls == 0
    new_id = o.new_doc_id

    assert all(r[9] is not None for r in _blocks(conn, old_id))  # 旧块只标 superseded_at，不删
    new = _blocks(conn, new_id)
    assert len(new) == len(before)
    assert sum(1 for a, b in zip(new, before, strict=True) if a[:5] != b[:5]) == 1
    assert {r[5] for r in new} == {"textin"}  # parse_source 写上了
    assert {r[8] for r in new} == {before[0][8]}  # known_at 沿用发布时刻
    leaves = [r for r in new if r[3]]
    assert o.embed_carried == len(leaves) - 1 and o.embed_needed == 1  # 改过的那块要重嵌

    doc = conn.execute(
        "SELECT superseded_at IS NULL, supersedes_doc_id, parse_json_ref FROM core.document"
        " WHERE doc_id = %s",
        (new_id,),
    ).fetchone()
    assert doc is not None and doc[0] and doc[1] == old_id
    (state,) = conn.execute(
        "SELECT state FROM core.parse_doc_acceptance WHERE routes_ref = %s", (doc[2],)
    ).fetchone()  # type: ignore[misc]
    assert state == o.state
    tables = conn.execute(
        "SELECT desc_model, desc_prompt_version FROM core.doc_table"
        " WHERE doc_id = %s AND superseded_at IS NULL",
        (new_id,),
    ).fetchall()
    assert tables and set(tables) == {("deepseek-chat", "tbl:v2")}
    assert check_schema_invariants(conn) == []


@pytest.mark.db
def test_a_second_run_skips_documents_already_on_the_new_flow(
    seeded: tuple[psycopg.Connection, LocalBlobStore, int],
) -> None:
    conn, blob, doc_id = seeded
    first = run_reparse_cached(conn, blob, doc_ids=None, write=True, ingest_run_id="t1")
    assert [o.status for o in first.outcomes] == ["filled"]
    second = run_reparse_cached(conn, blob, doc_ids=None, write=True, ingest_run_id="t2")
    assert second.outcomes == []
    assert [(n.doc_id, n.reason) for n in second.not_reparseable] == [(doc_id, "already_new_flow")]
    assert conn.execute("SELECT count(*) FROM core.document").fetchone() == (1,)


@pytest.mark.db
def test_documents_without_cache_are_listed_and_left_untouched(
    seeded: tuple[psycopg.Connection, LocalBlobStore, int], tmp_path: Path
) -> None:
    conn, _blob, doc_id = seeded
    empty = LocalBlobStore(tmp_path / "other")
    empty.put("raw/doc.pdf", _two_page_pdf() + b"%changed")
    report = run_reparse_cached(conn, empty, doc_ids=None, write=True, ingest_run_id="t")
    assert report.outcomes == []
    assert [(n.doc_id, n.reason) for n in report.not_reparseable] == [
        (doc_id, "textin_cache_missing")
    ]
    row = conn.execute("SELECT superseded_at FROM core.document WHERE doc_id = %s", (doc_id,))
    assert row.fetchone() == (None,)


@pytest.mark.db
def test_cli_defaults_to_dry_run_and_writes_a_report(
    seeded: tuple[psycopg.Connection, LocalBlobStore, int], temp_db: str, tmp_path: Path
) -> None:
    conn, _blob, _ = seeded
    out = tmp_path / "r.tsv"
    blob_root = tmp_path / "blob"
    r = CliRunner().invoke(
        main,
        ["docs", "reparse-cached", "--blob-root", str(blob_root), "--report", str(out)],
        env={"RAGDEMO_DSN": temp_db},
    )
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output
    assert out.read_text("utf-8").splitlines()[0].startswith("doc_id\t")
    assert conn.execute("SELECT count(*) FROM core.document").fetchone() == (1,)
    data = json.loads(FIXTURE.read_text("utf-8"))
    assert data["result"]["total_page_number"] == 2
