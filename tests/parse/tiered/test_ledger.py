# ruff: noqa: E501
# tests/parse/tiered/test_ledger.py
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.parse.tiered.ledger import (
    TierRow,
    estimate_page_cost,
    evaluate_ledger_alerts,
    format_tier_summary,
    record_parse_ledger,
    tier_summary,
)
from ragdemo.parse.tiered.records import DocRecord, PageRecord, ParseLedger
from ragdemo_core.db.migrate import migrate

P = Decimal("0.1356")
AT = datetime(2026, 11, 3, 10, tzinfo=UTC)
NOV = {"start": datetime(2026, 11, 1, tzinfo=UTC), "end": datetime(2026, 12, 1, tzinfo=UTC)}


def _l0(page: int) -> PageRecord:
    return PageRecord(
        page,
        "L0",
        "local_det:det-v2",
        "plain_text",
        tier_verdict="passed",
        p3_state="pass",
        output_numbers=1,
    )


def _l3(page: int, *, billed: bool = True) -> PageRecord:
    return PageRecord(
        page,
        "L3",
        "textin:1+fp",
        "table",
        ("L0 table/layout_unsupported",),
        textin_billed=billed,
        textin_json_ref="parse/textin/a/x.json",
        p3_state="pass",
        table_blocks=1,
        output_numbers=1,
    )


def _skip(page: int) -> PageRecord:
    return PageRecord(
        page,
        "skipped",
        "skip:blank_page",
        "no_text_layer",
        ("跳页：blank_page",),
        skip_rule="blank_page",
    )


def _doc(page_count: int, l0: int, l3: int, skipped: int, billed: int) -> DocRecord:
    return DocRecord("accepted", (), (), 0, 0, page_count, l0, l3, skipped, billed, None, "acc-v2")


def _ledger(ref: str = "parse/tiered/h/fp.json") -> ParseLedger:
    return ParseLedger(ref, (_l0(1), _l3(2), _skip(3)), _doc(3, 1, 1, 1, 1))


def test_records_reject_inconsistent_rows() -> None:
    with pytest.raises(ValueError):
        PageRecord(1, "skipped", "skip:x", "k")
    with pytest.raises(ValueError):
        PageRecord(1, "L0", "e", "k", textin_billed=True, p3_state="pass")
    with pytest.raises(ValueError):
        PageRecord(1, "L3", "e", "k")
    with pytest.raises(ValueError):
        PageRecord(1, "L3", "e", "k", p3_state="fail", unverified_numbers=2, output_numbers=1)
    with pytest.raises(ValueError):
        _doc(3, 1, 1, 0, 0)
    with pytest.raises(ValueError):
        ParseLedger("r", (_l0(2), _l0(1)), _doc(2, 2, 0, 0, 0))
    with pytest.raises(ValueError):
        ParseLedger("r", (_l0(1), _l3(2)), _doc(2, 2, 0, 0, 0))


def test_only_billed_textin_pages_cost_money() -> None:
    assert estimate_page_cost(_l0(1), P) == 0 and estimate_page_cost(_skip(3), P) == 0
    assert estimate_page_cost(_l3(2), P) == P
    assert estimate_page_cost(_l3(2, billed=False), P) == 0
    assert estimate_page_cost(_l3(2), None) is None


def _rows(**pages: int) -> list[TierRow]:
    return [TierRow(t, pages.get(t, 0), 0, Decimal(0), 0) for t in ("L0", "L3", "skipped")]  # type: ignore[arg-type]


def test_alerts() -> None:
    kw = {
        "fallback_docs": 0,
        "l0_unverified_pages": 0,
        "month_textin_cny": Decimal(0),
        "monthly_budget_cny": Decimal(5000),
    }
    assert [a.key for a in evaluate_ledger_alerts(_rows(L0=5, L3=60), **kw)] == [
        "parse_ledger:l3_share"
    ]  # type: ignore[arg-type]
    assert evaluate_ledger_alerts(_rows(L0=5, L3=30), **kw) == []  # type: ignore[arg-type]
    assert evaluate_ledger_alerts(_rows(L0=20, L3=40), **kw) == []  # type: ignore[arg-type]
    got = evaluate_ledger_alerts(
        _rows(L0=10),
        fallback_docs=2,
        l0_unverified_pages=1,
        month_textin_cny=Decimal(1500),
        monthly_budget_cny=Decimal(5000),
    )
    assert [(a.key, a.severity) for a in got] == [
        ("parse_ledger:l0_unverified", "critical"),
        ("parse_ledger:fallback", "warning"),
        ("parse_ledger:budget", "warning"),
    ]
    over = evaluate_ledger_alerts(
        _rows(),
        fallback_docs=0,
        l0_unverified_pages=0,
        month_textin_cny=Decimal(5000),
        monthly_budget_cny=Decimal(5000),
    )
    assert [(a.key, a.severity) for a in over] == [("parse_ledger:budget", "critical")]


def test_the_summary_renders_a_table_with_totals() -> None:
    rows = [
        TierRow("L0", 2, 0, Decimal(0), 0),
        TierRow("L3", 1, 1, P, 0),
        TierRow("skipped", 1, 0, Decimal(0), 0),
    ]
    text = format_tier_summary(rows)
    assert "| L3 | 1 | 1 | 0.1356 | 0 |" in text
    assert text.splitlines()[-1] == "| 合计 | 4 | 1 | 0.1356 | 0 |"


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, Path("db/migrations"))
    c.commit()
    return c


@pytest.mark.db
def test_ledger_rows_land_once_and_the_document_verdict_is_upserted(
    conn: psycopg.Connection,
) -> None:
    kw = {
        "content_hash": "h",
        "doc_type": "announcement",
        "ingest_run_id": "t",
        "routed_at": AT,
        "textin_per_page": P,
    }
    assert record_parse_ledger(conn, ledger=_ledger(), **kw) == 3  # type: ignore[arg-type]
    assert record_parse_ledger(conn, ledger=_ledger(), **kw) == 0  # type: ignore[arg-type]
    rows = {r.tier: r for r in tier_summary(conn, textin_per_page=P, **NOV)}
    assert (
        rows["L0"].pages,
        rows["L3"].pages,
        rows["L3"].billed_pages,
        rows["L3"].cost_cny,
        rows["skipped"].pages,
    ) == (1, 1, 1, P, 1)
    doc = conn.execute(
        "SELECT state, l0_pages, l3_pages, skipped_pages, billed_pages FROM core.parse_doc_acceptance"
        " WHERE routes_ref = %s",
        (_ledger().routes_ref,),
    ).fetchone()
    assert doc == ("accepted", 1, 1, 1, 1)
    checks = conn.execute(
        "SELECT skip_rule, shadow_skip_rules FROM core.parse_page_route WHERE page = 3"
    ).fetchone()
    assert checks == ("blank_page", [])


@pytest.mark.db
def test_unpriced_l3_pages_are_repriced_at_report_time(conn: psycopg.Connection) -> None:
    record_parse_ledger(
        conn,
        ledger=_ledger("r2"),
        content_hash="h",
        doc_type="announcement",
        ingest_run_id="t",
        routed_at=AT,
        textin_per_page=None,
    )
    assert {r.tier: r for r in tier_summary(conn, textin_per_page=P, **NOV)}["L3"].cost_cny == P
    assert {r.tier: r for r in tier_summary(conn, textin_per_page=None, **NOV)}[
        "L3"
    ].pages_unpriced == 1
