"""`docs render-html`：EDGAR htm 离线转 PDF，存成派生物并留来源快照（免费、不碰外部）。"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner
from pypdf import PdfReader

from ragdemo.adapters.mock.html_render import MockHtmlRenderer
from ragdemo.ingest.render_html import (
    RenderHtmlError,
    RenderOutcome,
    derived_pdf_key,
    render_html_document,
)
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
HTM = b"<html><body><p>8-K</p></body></html>"
RAW_REF = "edgar/0001045810/0001045810-26-000078/nvda-20260902.htm"


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    yield c
    c.close()


def insert_htm_doc(
    conn: psycopg.Connection,
    blob: LocalBlobStore,
    *,
    entity_id: str | None = None,
    raw_ref: str = RAW_REF,
    body: bytes = HTM,
) -> int:
    blob.put(raw_ref, body)
    publish_at = datetime(2026, 9, 3, 16, 3, 56, tzinfo=UTC)
    row = conn.execute(
        "INSERT INTO core.document (entity_id, doc_type, title, publish_at, language,"
        " source, source_url, raw_ref, content_hash, version_group_id, valid_from,"
        " known_at, source_ref, ingest_run_id) VALUES"
        " (%s,'8-K','8-K 0001045810-26-000078',%s,'en','edgar','https://sec.invalid/x.htm',"
        " %s,%s,0,%s,%s,'0001045810-26-000078','t') RETURNING doc_id",
        (
            entity_id,
            publish_at,
            raw_ref,
            hashlib.sha256(body).hexdigest(),
            publish_at.date(),
            publish_at,
        ),
    ).fetchone()
    assert row is not None
    conn.execute("UPDATE core.document SET version_group_id = doc_id WHERE doc_id = %s", (row[0],))
    conn.commit()
    return int(row[0])


def _render(
    conn: psycopg.Connection,
    blob: LocalBlobStore,
    doc_id: int,
    *,
    pages: int = 6,
    dry_run: bool = False,
) -> RenderOutcome:
    return render_html_document(
        conn,
        doc_id=doc_id,
        renderer=MockHtmlRenderer(pages=pages),
        blob=blob,
        ingest_run_id="t",
        dry_run=dry_run,
    )


def test_derived_key_naming() -> None:
    assert derived_pdf_key(RAW_REF) == (
        "edgar/0001045810/0001045810-26-000078/nvda-20260902.rendered.pdf"
    )
    assert derived_pdf_key("a/b.html") == "a/b.rendered.pdf"


def test_derived_key_rejects_non_html() -> None:
    with pytest.raises(RenderHtmlError):
        derived_pdf_key("a/b.pdf")


@pytest.mark.db
def test_render_writes_pdf_and_one_free_snapshot(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = insert_htm_doc(conn, blob)
    out = _render(conn, blob, doc_id)
    assert out.status == "rendered"
    key = derived_pdf_key(RAW_REF)
    pdf = blob.get(key)
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 6
    assert out.page_count == 6
    assert blob.get(RAW_REF) == HTM, "原始 htm 不动"

    rows = conn.execute(
        "SELECT provider, params, response_ref, cost_cents FROM core.provider_snapshot"
    ).fetchall()
    assert len(rows) == 1
    provider, params, response_ref, cost = rows[0]
    assert provider == "local-html-render"
    assert response_ref == key
    assert params["source_sha256"] == hashlib.sha256(HTM).hexdigest()
    assert params["pdf_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert params["source_raw_ref"] == RAW_REF
    assert params["page_count"] == 6
    assert params["renderer"]
    assert cost == 0
    assert out.snapshot_id is not None


@pytest.mark.db
def test_second_run_skips_and_never_overwrites(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = insert_htm_doc(conn, blob)
    _render(conn, blob, doc_id)
    before = blob.get(derived_pdf_key(RAW_REF))
    out = _render(conn, blob, doc_id, pages=2)  # 不同输出：若被覆盖字节会变
    assert out.status == "skipped"
    assert blob.get(derived_pdf_key(RAW_REF)) == before
    assert conn.execute("SELECT count(*) FROM core.provider_snapshot").fetchone() == (1,)


@pytest.mark.db
def test_existing_pdf_without_snapshot_gets_a_snapshot(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = insert_htm_doc(conn, blob)
    existing = MockHtmlRenderer(pages=3).render(HTM).pdf
    blob.put(derived_pdf_key(RAW_REF), existing)
    out = _render(conn, blob, doc_id, pages=9)
    assert out.status == "skipped"
    assert blob.get(derived_pdf_key(RAW_REF)) == existing
    row = conn.execute("SELECT params, cost_cents FROM core.provider_snapshot").fetchone()
    assert row is not None
    assert row[0]["pdf_sha256"] == hashlib.sha256(existing).hexdigest()
    assert row[0]["page_count"] == 3
    assert row[1] == 0


@pytest.mark.db
def test_dry_run_writes_nothing(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    doc_id = insert_htm_doc(conn, blob)
    out = _render(conn, blob, doc_id, dry_run=True)
    assert out.status == "dry_run"
    assert not blob.exists(derived_pdf_key(RAW_REF))
    assert conn.execute("SELECT count(*) FROM core.provider_snapshot").fetchone() == (0,)


@pytest.mark.db
def test_rejects_non_html_or_superseded_or_missing_docs(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    pdf_doc = insert_htm_doc(conn, blob, raw_ref="edgar/x/y/z.pdf", body=b"%PDF-1.4 a")
    with pytest.raises(RenderHtmlError):
        _render(conn, blob, pdf_doc)
    with pytest.raises(RenderHtmlError):
        _render(conn, blob, 9999)
    htm_doc = insert_htm_doc(conn, blob, raw_ref="edgar/a/b/c.htm", body=b"<html>other</html>")
    conn.execute("UPDATE core.document SET superseded_at = now() WHERE doc_id = %s", (htm_doc,))
    conn.commit()
    with pytest.raises(RenderHtmlError):
        _render(conn, blob, htm_doc)


@pytest.mark.db
def test_cli_render_html_dry_run_then_yes(
    temp_db: str, conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragdemo.cli import main

    blob = LocalBlobStore(tmp_path)
    doc_id = insert_htm_doc(conn, blob)
    monkeypatch.setenv("RAGDEMO_DSN", temp_db)
    runner = CliRunner()
    base = [
        "docs",
        "render-html",
        "--doc-id",
        str(doc_id),
        "--renderer",
        "mock",
        "--blob-root",
        str(tmp_path),
    ]
    r1 = runner.invoke(main, base)
    assert r1.exit_code == 0, r1.output
    assert "dry run" in r1.output
    assert not blob.exists(derived_pdf_key(RAW_REF))
    r2 = runner.invoke(main, [*base, "--yes"])
    assert r2.exit_code == 0, r2.output
    assert blob.exists(derived_pdf_key(RAW_REF))
    r3 = runner.invoke(main, [*base, "--yes"])
    assert r3.exit_code == 0 and "跳过" in r3.output
