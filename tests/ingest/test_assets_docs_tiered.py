# ruff: noqa: E501
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.ingest import assets_docs
from ragdemo.ingest.assets_docs import PreparedDocument, prepare_documents, record_parse_snapshots
from ragdemo.ingest.documents import ParseArtifacts
from ragdemo.ops.alerts import Alert
from ragdemo.parse.textin import MockDocumentParser, PageBudget, ParseResult
from ragdemo.parse.tiered.records import REASON_L3_NO_OUTPUT, DocRecord, PageRecord, ParseLedger
from ragdemo.parse.tiered.types import TextInCall
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate
from tests.ingest.test_assets_docs import _path_b_docs

REF = "parse/tiered/h/x.json"
CALLS = (
    TextInCall((1, 2), "parse/textin/a/x.json", None, "textin:1+fp", 2),
    TextInCall((4, 5), "parse/textin/b/x.json", None, "textin:1+fp", 0),
)
_OK: dict[str, Any] = {"tier_verdict": "not_run", "p3_state": "pass"}


def _l3(
    page: int,
    call: TextInCall,
    *,
    billed: bool,
    reasons: tuple[str, ...],
    kind: str = "plain_text",
    tables: int = 0,
) -> PageRecord:
    return PageRecord(
        page,
        "L3",
        "textin:1+fp",
        kind,
        reasons,
        textin_billed=billed,
        textin_json_ref=call.json_ref,
        table_blocks=tables,
        **_OK,
    )


def _ledger(*, no_output: bool = False) -> ParseLedger:
    off = ("L0 关闭",)
    pages = (
        _l3(1, CALLS[0], billed=True, reasons=off),
        _l3(2, CALLS[0], billed=True, reasons=off, kind="table", tables=1),
        PageRecord(
            3,
            "skipped",
            "skip:blank_page",
            "plain_text",
            ("跳页：blank_page",),
            skip_rule="blank_page",
        ),
        _l3(4, CALLS[1], billed=False, reasons=off + ((REASON_L3_NO_OUTPUT,) if no_output else ())),
        _l3(5, CALLS[1], billed=False, reasons=off),
    )
    return ParseLedger(
        REF, pages, DocRecord("accepted", (), (), 0, 0, 5, 0, 4, 1, 2, None, "acc-v2")
    )


class DocTypeAware:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, str | None]] = []

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        raise AssertionError("分页解析器必须走 parse_for")

    def parse_for(
        self, file_bytes: bytes, *, doc_type: str, title: str | None, owner_user: str | None = None
    ) -> ParseResult:
        self.seen.append((doc_type, title, owner_user))
        return ParseResult(
            [NormalizedBlock(0, "paragraph", "", "正文", page=1)],
            5,
            "tiered:v1+x",
            "",
            REF,
            None,
            [],
            False,
            billable_pages=2,
            textin_calls=CALLS,
            ledger=_ledger(),
        )


def test_prepare_documents_dispatches_by_doc_type_and_charges_billable_pages(
    tmp_path: Path,
) -> None:
    blob = LocalBlobStore(tmp_path)
    docs = _path_b_docs(blob, count=1)
    parser = DocTypeAware()
    budget = PageBudget(remaining=10)
    (item,) = prepare_documents(docs, parser, blob, budget, owner_user=None)
    assert parser.seen == [(docs[0].doc_type, docs[0].title, None)]
    assert (budget.remaining, item.billed_pages, item.textin_calls) == (8, 2, CALLS)
    assert item.ledger is not None and item.artifacts is not None and item.artifacts.json_ref == REF


def test_plain_parsers_keep_the_old_charge_rule(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    budget = PageBudget(remaining=1000)
    docs = _path_b_docs(blob, count=1)
    (item,) = prepare_documents(docs, MockDocumentParser(), blob, budget, owner_user=None)
    assert item.textin_calls == () and item.ledger is None


@pytest.mark.db
def test_each_billed_call_gets_a_snapshot_the_ledger_lands_and_empty_output_alerts(
    temp_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Alert] = []

    def fake_alert(conn: object, alert: Alert, **kw: object) -> dict[str, str]:
        sent.append(alert)
        return {}

    monkeypatch.setattr(assets_docs, "raise_alert", fake_alert)
    conn = psycopg.connect(temp_db)
    migrate(conn, Path("db/migrations"))
    conn.commit()
    doc = _path_b_docs(LocalBlobStore(tmp_path), count=1)[0]
    item = PreparedDocument(
        doc,
        ParseArtifacts(engine="tiered:v1+x", json_ref=REF),
        billed_pages=2,
        textin_calls=CALLS,
        ledger=_ledger(no_output=True),
    )
    ids = record_parse_snapshots(
        conn, [item], ingest_run_id="t", cost_per_page_cny=Decimal("0.1356")
    )
    assert len(ids) == sum(1 for c in CALLS if c.billed_pages > 0) == 1
    row = conn.execute(
        "SELECT cost_cents, params->>'billed_pages', response_ref FROM core.provider_snapshot WHERE snapshot_id = %s",
        (ids[0],),
    ).fetchone()
    assert row == (Decimal("27.1200"), "2", "parse/textin/a/x.json")
    assert conn.execute(
        "SELECT count(*) FROM core.parse_page_route WHERE routes_ref = %s", (REF,)
    ).fetchone() == (5,)
    assert conn.execute("SELECT state FROM core.parse_doc_acceptance").fetchone() == ("accepted",)
    assert [a.key for a in sent] == [f"parse_ledger:l3_no_output:{REF}"]
