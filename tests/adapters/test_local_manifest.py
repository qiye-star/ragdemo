"""local_manifest：manifest 行 → NormalizedDocument。

不测 AnnouncementProvider 契约——这个加载器刻意不实现那个协议
（见模块 docstring），所以这里只测它自己的行为。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ragdemo.adapters.local_manifest import (
    CST,
    ManifestError,
    count_pdf_pages,
    doc_type_for,
    known_at_from_manifest,
    load_documents,
)
from ragdemo_core.blob import LocalBlobStore


def _write_manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    manifest_path = tmp_path / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return manifest_path


def _pdf_bytes(marker: bytes = b"fake pdf content") -> bytes:
    return b"%PDF-1.4\n" + marker


def _entry(
    tmp_path: Path,
    *,
    local_path: str = "announcements/a.pdf",
    title: str = "海光信息技术股份有限公司关于某事项的公告",
    known_at: str = "2026-04-08 00:00:00",
    kind: str = "announcement",
    data: bytes | None = None,
) -> dict[str, object]:
    data = data if data is not None else _pdf_bytes(local_path.encode())
    file_path = tmp_path / local_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(data)
    return {
        "kind": kind,
        "entity_code": "688041",
        "entity_name": "海光信息",
        "title": title,
        "known_at": known_at,
        "source": "cninfo",
        "source_id": "1000000001",
        "url": "http://static.cninfo.com.cn/finalpage/x.PDF",
        "local_path": local_path,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "org": "",
        "extra": {"columns": "251302", "type": None},
    }


# --- doc_type 映射 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("海光信息技术股份有限公司2025年年度报告摘要", "annual_report"),
        ("海光信息技术股份有限公司2025年年度报告", "annual_report"),
        ("海光信息技术股份有限公司2026年第一季度报告", "quarterly"),
        ("海光信息技术股份有限公司2026年半年度报告摘要", "quarterly"),
        ("海光信息技术股份有限公司关于回购股份进展的公告", "announcement"),
    ],
)
def test_doc_type_for(title: str, expected: str) -> None:
    assert doc_type_for(title) == expected


# --- known_at：日期 + 23:59:59+08:00 ----------------------------------------


def test_known_at_lands_at_23_59_59_plus_8() -> None:
    known_at = known_at_from_manifest("2026-04-08 00:00:00")
    assert known_at.tzinfo is not None
    assert known_at.utcoffset() is not None
    assert known_at.astimezone(CST) == known_at  # 已经是 CST 表示
    assert (known_at.hour, known_at.minute, known_at.second) == (23, 59, 59)
    assert known_at.date().isoformat() == "2026-04-08"


def test_known_at_never_earlier_than_naive_midnight_utc8() -> None:
    """00:00+08:00 会让 known_at 提早最多 24 小时——这是要防的前视方向。"""
    naive_midnight_cst = known_at_from_manifest("2026-04-08 00:00:00").replace(
        hour=0, minute=0, second=0
    )
    known_at = known_at_from_manifest("2026-04-08 00:00:00")
    assert known_at >= naive_midnight_cst


# --- sha256 校验 --------------------------------------------------------------


def test_sha256_mismatch_is_rejected(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    entry["sha256"] = "0" * 64  # 故意错误
    manifest_path = _write_manifest(tmp_path, [entry])
    blob = LocalBlobStore(tmp_path / "blob")

    with pytest.raises(ManifestError, match="sha256"):
        load_documents(manifest_path, blob, entity_ref="688041.SH")


def test_matching_sha256_is_accepted(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    manifest_path = _write_manifest(tmp_path, [entry])
    blob = LocalBlobStore(tmp_path / "blob")

    docs = load_documents(manifest_path, blob, entity_ref="688041.SH")
    assert len(docs) == 1
    assert docs[0].content_hash == entry["sha256"]


# --- 端到端加载 ---------------------------------------------------------------


def test_load_documents_sets_entity_ref_explicitly_not_from_manifest(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    manifest_path = _write_manifest(tmp_path, [entry])
    blob = LocalBlobStore(tmp_path / "blob")

    docs = load_documents(manifest_path, blob, entity_ref="688041.SH")
    assert docs[0].entity_ref == "688041.SH"  # 不是 manifest 里的 entity_code "688041"


def test_load_documents_puts_bytes_into_blob_before_referencing_them(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    manifest_path = _write_manifest(tmp_path, [entry])
    blob = LocalBlobStore(tmp_path / "blob")

    docs = load_documents(manifest_path, blob, entity_ref="688041.SH")
    doc = docs[0]
    assert doc.raw_bytes_ref is not None
    assert blob.exists(doc.raw_bytes_ref)
    assert blob.get(doc.raw_bytes_ref) == (tmp_path / "announcements/a.pdf").read_bytes()


def test_load_documents_skips_research_kind(tmp_path: Path) -> None:
    ann = _entry(tmp_path, local_path="announcements/a.pdf")
    research = _entry(tmp_path, local_path="research/r.pdf", kind="research")
    manifest_path = _write_manifest(tmp_path, [ann, research])
    blob = LocalBlobStore(tmp_path / "blob")

    docs = load_documents(manifest_path, blob, entity_ref="688041.SH")
    assert len(docs) == 1
    assert docs[0].provider_doc_id == ann["source_id"]


def test_load_documents_filters_by_select_title(tmp_path: Path) -> None:
    a = _entry(tmp_path, local_path="announcements/a.pdf", title="标题一：年度报告摘要")
    b = _entry(tmp_path, local_path="announcements/b.pdf", title="标题二：回购公告")
    manifest_path = _write_manifest(tmp_path, [a, b])
    blob = LocalBlobStore(tmp_path / "blob")

    docs = load_documents(manifest_path, blob, entity_ref="688041.SH", select_title="年度报告摘要")
    assert len(docs) == 1
    assert docs[0].title == a["title"]


def test_normalized_document_has_empty_blocks_pending_parse(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    manifest_path = _write_manifest(tmp_path, [entry])
    blob = LocalBlobStore(tmp_path / "blob")

    docs = load_documents(manifest_path, blob, entity_ref="688041.SH")
    assert docs[0].blocks == []
    assert docs[0].page_count is None


# --- 本地页数（不调用任何 API 就能算出来的估算） -------------------------------


def test_count_pdf_pages_handles_simple_pdf() -> None:
    # 最小合法 PDF：一个 Pages 根 + 两个 Page 对象，未压缩。
    pdf = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R>>endobj
4 0 obj<</Type/Page/Parent 2 0 R>>endobj
trailer<</Root 1 0 R>>
"""
    assert count_pdf_pages(pdf) == 2


def test_count_pdf_pages_returns_zero_for_non_pdf_bytes() -> None:
    assert count_pdf_pages(b"not a pdf at all") == 0
