"""`ragdemo docs` CLI：本地 manifest → 解析 → 切块 → 入库。

按页计费下的核心不变量——dry run 零调用、预算耗尽止步于文档之间、
entity_ref 显式解析、content_desc 陷阱有名有姓——都在这里验证。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner, Result

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.adapters.local_manifest import CST
from ragdemo.cli import main
from ragdemo.ingest import cli as docs_cli
from ragdemo.parse.chunker import Chunk
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.textin import ParseResult
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def _pdf_with_pages(n: int) -> bytes:
    """构造一份最小合法 PDF，恰好含 n 个 /Type/Page 对象——用于让本地页数估算可预测。"""
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {n}>>endobj".encode(),
    ]
    for i in range(n):
        objs.append(f"{3 + i} 0 obj<</Type/Page/Parent 2 0 R>>endobj".encode())
    return b"%PDF-1.4\n" + b"\n".join(objs) + b"\ntrailer<</Root 1 0 R>>\n"


def _write_manifest(
    tmp_path: Path,
    *,
    count: int,
    title_prefix: str = "海光信息技术股份有限公司关于事项",
    pages_per_doc: int = 3,
) -> Path:
    manifest_path = tmp_path / "manifest.jsonl"
    ann_dir = tmp_path / "announcements"
    ann_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(count):
        data = _pdf_with_pages(pages_per_doc)
        local_path = f"announcements/doc-{i}.pdf"
        (tmp_path / local_path).write_bytes(data)
        lines.append(
            {
                "kind": "announcement",
                "entity_code": "688041",
                "entity_name": "海光信息",
                "title": f"{title_prefix}{i}的公告",
                "known_at": "2026-04-08 00:00:00",
                "source": "cninfo",
                "source_id": f"100000000{i}",
                "url": "http://static.cninfo.com.cn/finalpage/x.PDF",
                "local_path": local_path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "org": "",
                "extra": {},
            }
        )
    manifest_path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return manifest_path


# --- content_desc 陷阱：纯函数级别，不需要 CLI 或数据库 ------------------------


def test_describe_tables_downgrades_rejection_instead_of_raising() -> None:
    """真实字段名撞上违禁词过滤（如"评级"作为合法表格列名）不能让流程崩掉。"""
    chunk = Chunk(
        ordinal=5,
        block_type="table",
        section_path="第一节",
        content="| 评级 | 数值 |\n|---|---|\n| A | 1 |",
        page=1,
        bbox=None,
        is_leaf=True,
    )
    descriptions, rejections = docs_cli.describe_tables([chunk], MockTableDescriber(), title="标题")
    assert 5 not in descriptions
    assert 5 in rejections
    assert "评级" in rejections[5] or "违禁词" in rejections[5]


def test_format_violations_names_the_block_and_the_reason() -> None:
    violations = ["块 5: 表格块缺少 content_desc"]
    rejections = {5: "描述含违禁词: (买入|卖出|...|评级|...)"}
    formatted = docs_cli.format_violations(violations, rejections)
    assert len(formatted) == 1
    assert "块 5" in formatted[0]
    assert "描述生成被拒绝" in formatted[0]
    assert "评级" in formatted[0]


def test_build_chunks_and_descriptions_returns_empty_for_unparsed_document() -> None:
    doc = NormalizedDocument(
        provider_doc_id="P-1",
        entity_ref="688041.SH",
        doc_type="announcement",
        title="未解析文档",
        period=None,
        publish_at=datetime(2026, 4, 8, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref="raw/x.pdf",
        content_hash="hash",
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=None,
        blocks=[],
    )
    chunks, descriptions, violations = docs_cli.build_chunks_and_descriptions(
        doc, ChunkConfig(), MockTableDescriber()
    )
    assert chunks == []
    assert descriptions == {}
    assert violations == []


# --- CLI 层：dry run 的零调用与零违规不变量 ------------------------------------


def test_docs_subcommands_exist() -> None:
    result = CliRunner().invoke(main, ["docs", "--help"])
    assert result.exit_code == 0
    for cmd in ("ingest", "rechunk", "embed"):
        assert cmd in result.output


def test_dry_run_never_constructs_a_real_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingTextInParser:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("dry run 不应该构造 TextInParser")

    monkeypatch.setattr(docs_cli, "TextInParser", ExplodingTextInParser)
    manifest_path = _write_manifest(tmp_path, count=1)

    result = CliRunner().invoke(
        main,
        [
            "docs",
            "ingest",
            "--manifest",
            str(manifest_path),
            "--entity-ref",
            "688041.SH",
            "--parser",
            "textin",
            "--blob-root",
            str(tmp_path / "blob"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output


def test_dry_run_does_not_require_ragdemo_dsn(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, count=1)
    result = CliRunner().invoke(
        main,
        [
            "docs",
            "ingest",
            "--manifest",
            str(manifest_path),
            "--entity-ref",
            "688041.SH",
            "--blob-root",
            str(tmp_path / "blob"),
        ],
        env={"RAGDEMO_DSN": ""},
    )
    assert result.exit_code == 0, result.output


def test_dry_run_reports_local_page_counts_and_total_charge(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, count=2, pages_per_doc=5)
    result = CliRunner().invoke(
        main,
        [
            "docs",
            "ingest",
            "--manifest",
            str(manifest_path),
            "--entity-ref",
            "688041.SH",
            "--blob-root",
            str(tmp_path / "blob"),
            "--max-pages",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "~5 页" in result.output
    assert "预计消耗页数 ~= 10" in result.output
    assert "预算 3 页" in result.output
    assert "超出本次页数预算" in result.output


def test_dry_run_with_mock_parser_surfaces_content_desc_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RejectingParser:
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
            blocks = [
                NormalizedBlock(
                    0, "table", "第一节", "| 评级 | 数值 |\n|---|---|\n| A | 1 |", page=1
                )
            ]
            return ParseResult(
                blocks=blocks,
                page_count=1,
                engine_version="mock:1",
                markdown="",
                json_ref=None,
                md_ref=None,
            )

    monkeypatch.setattr(docs_cli, "MockDocumentParser", RejectingParser)
    manifest_path = _write_manifest(tmp_path, count=1)

    result = CliRunner().invoke(
        main,
        [
            "docs",
            "ingest",
            "--manifest",
            str(manifest_path),
            "--entity-ref",
            "688041.SH",
            "--parser",
            "mock",
            "--blob-root",
            str(tmp_path / "blob"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "[违规]" in result.output
    assert "表格块缺少 content_desc" in result.output
    assert "描述生成被拒绝" in result.output


# --- CLI 层：真正写库（--yes），需要数据库 -------------------------------------


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688041','海光信息技术股份有限公司','listed','算力','AI芯片',"
        " ARRAY['通用服务器CPU'],'通用服务器CPU','688041.SH')"
    )
    # 'cninfo' 是 --source 的默认值（ingest/cli.py），这里只是测试夹具里的
    # 一个占位登记——真实供应商的四条款要等商务确认后由运维登记
    # （docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 B）。
    c.execute(
        "INSERT INTO core.source_registry (source_id, vendor, layer, can_cache,"
        " can_show_raw, can_vectorize, time_precision) "
        "VALUES ('cninfo','cninfo','filing',true,true,true,'second')"
    )
    c.commit()
    return c


def _invoke_yes(
    manifest_path: Path,
    blob_root: Path,
    dsn: str,
    *,
    max_pages: int,
    extra: list[str] | None = None,
) -> Result:
    args = [
        "docs",
        "ingest",
        "--manifest",
        str(manifest_path),
        "--entity-ref",
        "688041.SH",
        "--parser",
        "mock",
        "--blob-root",
        str(blob_root),
        "--max-pages",
        str(max_pages),
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(main, args, env={"RAGDEMO_DSN": dsn})


@pytest.mark.db
def test_yes_ingest_writes_documents_and_resolves_entity_ref(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str
) -> None:
    manifest_path = _write_manifest(tmp_path, count=1)
    result = _invoke_yes(manifest_path, tmp_path / "blob", temp_db, max_pages=500)
    assert result.exit_code == 0, result.output

    rows = conn.execute(
        "SELECT doc_id, doc_type, parse_engine, entity_id, known_at FROM core.document"
    ).fetchall()
    assert len(rows) == 1
    _doc_id, _doc_type, parse_engine, entity_id, known_at = rows[0]
    assert parse_engine.startswith("mock:")
    assert entity_id == "CN.688041"
    # psycopg 按连接会话时区把 timestamptz 转回来（这里是 UTC），断言前转回
    # CST——落库的绝对时刻才是我们要核实的东西，不是它被驱动打印成什么样。
    known_at_cst = known_at.astimezone(CST)
    assert (known_at_cst.hour, known_at_cst.minute, known_at_cst.second) == (23, 59, 59)


@pytest.mark.db
def test_budget_exhaustion_stops_mid_list_not_mid_document(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str
) -> None:
    """MockDocumentParser 每份文档固定收 2 页；预算 3 页只够处理第一份。"""
    manifest_path = _write_manifest(tmp_path, count=3)
    result = _invoke_yes(manifest_path, tmp_path / "blob", temp_db, max_pages=3)
    assert result.exit_code == 0, result.output

    (count,) = conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert count == 1  # 第一份用掉 2 页，预算见底，后两份完全没有被处理


@pytest.mark.db
def test_content_desc_rejection_fails_only_that_document(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RejectingParser:
        def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
            blocks = [
                NormalizedBlock(
                    0, "table", "第一节", "| 评级 | 数值 |\n|---|---|\n| A | 1 |", page=1
                )
            ]
            return ParseResult(
                blocks=blocks,
                page_count=1,
                engine_version="mock:1",
                markdown="",
                json_ref=None,
                md_ref=None,
            )

    monkeypatch.setattr(docs_cli, "MockDocumentParser", RejectingParser)
    manifest_path = _write_manifest(tmp_path, count=1)
    result = _invoke_yes(manifest_path, tmp_path / "blob", temp_db, max_pages=500)

    assert result.exit_code != 0
    assert "表格块缺少 content_desc" in result.output
    assert "描述生成被拒绝" in result.output
    (count,) = conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert count == 0  # 整份文档没有落进任何一行——不是部分写入


@pytest.mark.db
def test_rerunning_yes_ingest_is_a_cache_hit_and_skips(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str
) -> None:
    manifest_path = _write_manifest(tmp_path, count=1)
    blob_root = tmp_path / "blob"
    first = _invoke_yes(manifest_path, blob_root, temp_db, max_pages=500)
    assert first.exit_code == 0, first.output

    second = _invoke_yes(manifest_path, blob_root, temp_db, max_pages=500)
    assert second.exit_code == 0, second.output
    assert "跳过(已存在)" in second.output

    (count,) = conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert count == 1


# --- docs embed ---------------------------------------------------------------


@pytest.mark.db
def test_docs_embed_fills_in_missing_vectors(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str
) -> None:
    manifest_path = _write_manifest(tmp_path, count=1)
    ingested = _invoke_yes(manifest_path, tmp_path / "blob", temp_db, max_pages=500)
    assert ingested.exit_code == 0, ingested.output

    (pending_before,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert pending_before > 0

    result = CliRunner().invoke(main, ["docs", "embed"], env={"RAGDEMO_DSN": temp_db})
    assert result.exit_code == 0, result.output
    assert f"pending={pending_before}" in result.output

    (pending_after,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE is_leaf AND embedding IS NULL"
    ).fetchone()  # type: ignore[misc]
    assert pending_after == 0


# --- docs rechunk --------------------------------------------------------------


@pytest.mark.db
def test_docs_rechunk_reuses_cached_parse_with_zero_api_calls(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str
) -> None:
    from ragdemo.parse.textin import XPARSE_PARAMS, TextInParser, artifact_keys, param_fingerprint
    from ragdemo_core.blob import LocalBlobStore

    manifest_path = _write_manifest(tmp_path, count=1)
    blob_root = tmp_path / "blob"
    blob = LocalBlobStore(blob_root)

    # 预先把一份真实 xParse 响应放进缓存，让 --parser textin 在这次 --yes
    # 入库时走缓存命中分支——不发一次网络请求（TextInParser.parse() 的既有
    # 行为），但落库的 parse_engine 从此是 "textin:..." 而不是 "mock:1"，
    # 使 docs rechunk 的缓存查找条件与它保持一致。
    raw_bytes = (tmp_path / "announcements/doc-0.pdf").read_bytes()
    content_hash = TextInParser.content_hash(raw_bytes)
    param_fp = param_fingerprint(XPARSE_PARAMS)
    json_key, _ = artifact_keys(content_hash, param_fp)
    fixture = json.loads(Path("tests/fixtures/xparse/annual_report.json").read_text("utf-8"))
    blob.put(json_key, json.dumps(fixture, ensure_ascii=False).encode("utf-8"))

    ingested = CliRunner().invoke(
        main,
        [
            "docs",
            "ingest",
            "--manifest",
            str(manifest_path),
            "--entity-ref",
            "688041.SH",
            "--parser",
            "textin",
            "--blob-root",
            str(blob_root),
            "--yes",
        ],
        env={"RAGDEMO_DSN": temp_db, "TEXTIN_BASE_URL": "http://unused.invalid"},
    )
    assert ingested.exit_code == 0, ingested.output

    (doc_id, parse_engine) = conn.execute(
        "SELECT doc_id, parse_engine FROM core.document"
    ).fetchone()  # type: ignore[misc]
    assert parse_engine.startswith("textin:")

    rechunked = CliRunner().invoke(
        main,
        [
            "docs",
            "rechunk",
            "--doc-id",
            str(doc_id),
            "--entity-ref",
            "688041.SH",
            "--blob-root",
            str(blob_root),
        ],
        env={"RAGDEMO_DSN": temp_db},
    )
    assert rechunked.exit_code == 0, rechunked.output
    assert "重新切块完成" in rechunked.output

    rows = conn.execute(
        "SELECT doc_id, superseded_at FROM core.document ORDER BY doc_id"
    ).fetchall()
    assert len(rows) == 2  # 旧版本仍在，只是被标记为已取代
    assert rows[0][0] == doc_id
    assert rows[0][1] is not None  # 旧行的 superseded_at 已经写上
    assert rows[1][1] is None  # 新行仍然是活跃版本


@pytest.mark.db
def test_docs_rechunk_refuses_when_nothing_is_cached(
    conn: psycopg.Connection, tmp_path: Path, temp_db: str
) -> None:
    manifest_path = _write_manifest(tmp_path, count=1)
    ingested = _invoke_yes(manifest_path, tmp_path / "blob", temp_db, max_pages=500)
    assert ingested.exit_code == 0, ingested.output

    (doc_id,) = conn.execute("SELECT doc_id FROM core.document").fetchone()  # type: ignore[misc]

    # 这份文档是用 --parser mock 写进去的，从没有真实 xParse JSON 落过盘，
    # blob 缓存里没有可复用的解析结果——rechunk 必须拒绝，而不是退化成
    # 悄悄触发一次新的计费解析。
    result = CliRunner().invoke(
        main,
        [
            "docs",
            "rechunk",
            "--doc-id",
            str(doc_id),
            "--entity-ref",
            "688041.SH",
            "--blob-root",
            str(tmp_path / "blob"),
        ],
        env={"RAGDEMO_DSN": temp_db},
    )
    assert result.exit_code != 0
    assert "没有已归档的解析结果" in result.output
