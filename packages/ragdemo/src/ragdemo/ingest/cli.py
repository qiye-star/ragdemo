"""`ragdemo docs` 组：本地 manifest → 解析 → 切块 → 入库 → 嵌入（Phase 1.2 组装入口）。

在这个改动之前，`TextInParser` 与 `LocalBlobStore` 只出现在 `tests/` 下，
`TEXTIN_*` 三个环境变量没有任何代码读取——文档管线代码齐全、评审过，
但从没有被组装成一个能跑的入口。这个模块就是那个入口，而且**只是编排**：
它调用与 Dagster 资产完全相同的函数——`prepare_documents`
（`ingest/assets_docs.py`）→ `chunk_document`（`parse/chunker.py`）→
`build_tree`（`parse/tree.py`）→ `MockTableDescriber().describe()`
（`parse/describe.py`）→ `DocumentWriter.write_document` /
`reparse_document`（`ingest/documents.py`）→ `embed_pending_blocks`
（`embed/batch.py`）——不是第二套实现。

解析器按页计费，这里因此有两条不可协商的规则：

1. **dry run 是默认行为**。不传 `--yes` 就只读本地文件（manifest、PDF 字节、
   sha256 校验、本地页数估算），**绝不构造** `TextInParser`，也绝不连数据库。
   `--parser mock` 除外：`MockDocumentParser` 本身不发网络请求，在 dry run
   里用它跑一遍 `chunk_document → build_tree → validate_chunks` 是完全免费的，
   而且正是"content_desc 陷阱"（见 `describe_tables` 的文档字符串）能在
   花钱之前被发现的地方。
2. **只有 `--yes` 才会真正调用解析器或写库**。
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.local_manifest import ManifestError, count_pdf_pages, load_documents
from ragdemo.config import ConfigError, load_config
from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo.ingest.assets_docs import prepare_documents
from ragdemo.ingest.documents import DocumentWriter, MetadataInvalid, ParseArtifacts
from ragdemo.parse.chunker import Chunk, chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import DescriptionRejected, MockTableDescriber, TableDescriber
from ragdemo.parse.textin import (
    XPARSE_PARAMS,
    DocumentParser,
    MockDocumentParser,
    PageBudget,
    TextInParser,
    artifact_keys,
    param_fingerprint,
)
from ragdemo.parse.tree import build_tree
from ragdemo.parse.validate import validate_chunks
from ragdemo_core.blob import BlobStore, LocalBlobStore

_MISSING_DESC_RE = re.compile(r"^块 (\d+): 表格块缺少 content_desc$")


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


def _load_config() -> Any:  # noqa: ANN401
    try:
        return load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group("docs")
def docs_group() -> None:
    """本地公告 manifest 的解析、入库与嵌入。"""


# --- 公共编排：dry run 与真实入库共用，两条路径必须看见同一套判断 -------------


def describe_tables(
    chunks: list[Chunk], describer: TableDescriber, *, title: str
) -> tuple[dict[int, str], dict[int, str]]:
    """对每个表格块调用一次 `describer.describe()`。

    **这是"content_desc 陷阱"真正的修复点。** `MockTableDescriber` 从真实
    年报的表头列名里直接拼描述；真实财报里"说明"（备注列）、"预计负债"
    （会计科目）这类合法列名会撞上 `describe.py::validate_description` 的
    解读词/违禁词过滤而被拒绝（代码注释里写着已经为这两个词patch 过两次）。
    `ingest/assets_docs.py::doc_blocks_loaded` 对这个异常没有任何保护——
    `DescriptionRejected` 会直接从字典推导式里冒出来，炸穿整个 run，而且
    异常信息不会提到是哪个块、哪份文档。这里把它降级成"这个块没有
    content_desc"（不放进 `descriptions`），交给 `validate_chunks` 给出
    统一、按块号定位的违规文案，同时把拒绝原因单独记下来（第二个返回值），
    配合 `format_violations` 说清楚"哪个块、为什么"。
    """
    descriptions: dict[int, str] = {}
    rejections: dict[int, str] = {}
    for chunk in chunks:
        if chunk.block_type != "table":
            continue
        try:
            descriptions[chunk.ordinal] = describer.describe(
                chunk.content, title=title, section_path=chunk.section_path
            )
        except DescriptionRejected as exc:
            rejections[chunk.ordinal] = str(exc)
    return descriptions, rejections


def format_violations(violations: list[str], rejections: dict[int, str]) -> list[str]:
    """把 `validate_chunks` 里"缺少 content_desc"的违规行接上具体的拒绝原因。"""
    formatted: list[str] = []
    for violation in violations:
        match = _MISSING_DESC_RE.match(violation)
        if match is not None and int(match.group(1)) in rejections:
            reason = rejections[int(match.group(1))]
            formatted.append(f"{violation}（描述生成被拒绝：{reason}）")
        else:
            formatted.append(violation)
    return formatted


def build_chunks_and_descriptions(
    doc: NormalizedDocument, cfg: ChunkConfig, describer: TableDescriber
) -> tuple[list[Chunk], dict[int, str], list[str]]:
    """`chunk_document` → `build_tree` → 表格描述 → `validate_chunks` 的公共编排。

    真正写库（`docs ingest --yes`）与 dry run 的 content_desc 预检共用这一段
    ——两条路径必须看到完全一致的违规判断，不能各写一份，否则会出现
    "dry run 说没问题，真正跑起来却回滚"的漂移。没有块（还没解析，或解析
    被判定为永久失败/预算耗尽之外的空文档）时直接返回空结果，与
    `ingest/assets_docs.py::doc_blocks_loaded` 原有行为一致。
    """
    if not doc.blocks:
        return [], {}, []
    chunks = build_tree(chunk_document(doc, cfg))
    descriptions, rejections = describe_tables(chunks, describer, title=doc.title)
    violations = format_violations(validate_chunks(doc, chunks, descriptions), rejections)
    return chunks, descriptions, violations


# --- dry run -----------------------------------------------------------------


def _dry_run(
    docs: list[NormalizedDocument],
    blob: BlobStore,
    *,
    chunk_cfg: ChunkConfig,
    describer: TableDescriber,
    parser_kind: str,
    max_pages: int,
) -> None:
    click.echo(f"[dry run] 命中 {len(docs)} 份文档，未发起任何计费调用")
    total_pages = 0
    any_violation = False
    # MockDocumentParser 不发网络请求，dry run 里用它预览完全免费；
    # --parser textin 时绝不构造 TextInParser——即使命中缓存能免费预检，
    # 把这条路走通需要在 dry run 里持有一个"可能发起计费调用"的类，
    # 任何未来的改动都有可能不小心把它变成真的会花钱，不值得冒这个险。
    preview_parser = MockDocumentParser() if parser_kind == "mock" else None

    for doc in docs:
        raw_bytes = blob.get(doc.raw_bytes_ref) if doc.raw_bytes_ref else b""
        local_pages = count_pdf_pages(raw_bytes) if raw_bytes else 0
        total_pages += local_pages
        click.echo(f"  - [{doc.doc_type}] {doc.title} (~{local_pages} 页)")

        if preview_parser is None:
            continue
        result = preview_parser.parse(raw_bytes, owner_user=None)
        previewed = replace(doc, blocks=result.blocks, page_count=result.page_count)
        _, _, violations = build_chunks_and_descriptions(previewed, chunk_cfg, describer)
        if violations:
            any_violation = True
            click.echo(f"    [违规] {doc.title}:")
            for violation in violations:
                click.echo(f"      - {violation}")

    over_budget = " (超出本次页数预算，--yes 执行时会提前停止)" if total_pages > max_pages else ""
    click.echo(f"[dry run] 预计消耗页数 ~= {total_pages}，本次预算 {max_pages} 页{over_budget}")
    if parser_kind != "mock":
        click.echo(
            "[dry run] --parser textin 下不预检 content_desc（不构造 TextInParser，"
            "避免任何误触发计费调用的可能）；改用 --parser mock 可以免费预检切块与描述。"
        )
    if any_violation:
        click.echo(
            "[dry run] 发现校验违规：真正执行（--yes）会导致对应文档整体回滚，建议先修复。",
            err=True,
        )


# --- docs ingest --------------------------------------------------------------


@docs_group.command("ingest")
@click.option(
    "--manifest",
    "manifest_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--entity-ref",
    required=True,
    help="显式实体代码，如 688041.SH。不从 manifest 的 entity_code 猜——"
    "entity_alias 不参与 DocumentWriter 的实体解析，猜错会静默写成 entity_id=NULL。",
)
@click.option("--select-title", default=None, help="按标题子串筛选；不传则选中全部公告")
@click.option(
    "--max-pages",
    "max_pages_opt",
    type=int,
    default=None,
    help="本次 run 的页数预算；不传则取 TEXTIN_MAX_PAGES_PER_RUN",
)
@click.option("--yes", is_flag=True, default=False, help="真正调用解析器并写库；不传则只 dry run")
@click.option(
    "--parser",
    "parser_kind",
    type=click.Choice(["textin", "mock"]),
    default="textin",
    help="mock 用于离线开发与测试，完全不发网络请求",
)
@click.option("--blob-root", "blob_root_opt", type=click.Path(path_type=Path), default=None)
@click.option("--source", "source_name", default="cninfo", help="写入 core.document.source 的值")
def docs_ingest(
    manifest_path: Path,
    entity_ref: str,
    select_title: str | None,
    max_pages_opt: int | None,
    yes: bool,
    parser_kind: str,
    blob_root_opt: Path | None,
    source_name: str,
) -> None:
    """本地公告 manifest → 解析 → 切块 → 入库。默认 dry run；--yes 才真正花钱写库。"""
    cfg = _load_config()
    blob = LocalBlobStore(blob_root_opt or cfg.blob_root)
    max_pages = max_pages_opt if max_pages_opt is not None else cfg.textin_max_pages_per_run

    try:
        docs = load_documents(manifest_path, blob, entity_ref=entity_ref, select_title=select_title)
    except ManifestError as exc:
        raise click.ClickException(str(exc)) from exc

    if not docs:
        raise click.ClickException("manifest 里没有匹配的公告，检查 --select-title / --manifest")

    chunk_cfg = ChunkConfig()
    describer = MockTableDescriber()

    if not yes:
        _dry_run(
            docs,
            blob,
            chunk_cfg=chunk_cfg,
            describer=describer,
            parser_kind=parser_kind,
            max_pages=max_pages,
        )
        return

    parser: DocumentParser
    if parser_kind == "mock":
        parser = MockDocumentParser()
    else:
        base_url = cfg.require_textin_base_url()
        parser = TextInParser(base_url, blob, allow_private=cfg.textin_allow_private)

    budget = PageBudget(max_pages)
    failures = 0
    with psycopg.connect(_dsn()) as conn:
        writer = DocumentWriter(conn, ingest_run_id="cli:docs-ingest", source=source_name)
        prepared = prepare_documents(docs, parser, blob, budget, owner_user=None)
        for item in prepared:
            chunks, descriptions, violations = build_chunks_and_descriptions(
                item.doc, chunk_cfg, describer
            )
            if violations:
                failures += 1
                click.echo(f"[失败] {item.doc.title}:", err=True)
                for violation in violations:
                    click.echo(f"  - {violation}", err=True)
                continue
            try:
                result = writer.write_document(
                    item.doc,
                    chunks,
                    descriptions,
                    owner_user=item.owner_user,
                    artifacts=item.artifacts,
                )
            except MetadataInvalid as exc:
                failures += 1
                click.echo(f"[失败] {item.doc.title}: {exc}", err=True)
                continue
            status = "跳过(已存在)" if result.skipped else "写入"
            click.echo(
                f"{status} doc_id={result.doc_id} blocks={len(result.block_ids)} {item.doc.title}"
            )

    if failures:
        raise click.ClickException(f"{failures} 份文档未能入库，见上方错误")


# --- docs rechunk --------------------------------------------------------------


@docs_group.command("rechunk")
@click.option("--doc-id", "doc_id", required=True, type=int)
@click.option(
    "--entity-ref",
    required=True,
    help="与原文档一致的显式实体代码；重新解析不改变文档归属，只是换一套切块结果",
)
@click.option("--blob-root", "blob_root_opt", type=click.Path(path_type=Path), default=None)
def docs_rechunk(doc_id: int, entity_ref: str, blob_root_opt: Path | None) -> None:
    """从已归档的 xParse JSON 重新切块（zero API calls），用于 ChunkConfig 调参。

    只走 blob 缓存命中路径：命中检查在调用 `TextInParser.parse()` 之前就做完，
    没有缓存直接拒绝，不会退化成一次真的计费解析。
    """
    cfg = _load_config()
    blob = LocalBlobStore(blob_root_opt or cfg.blob_root)
    chunk_cfg = ChunkConfig()
    describer = MockTableDescriber()

    with psycopg.connect(_dsn()) as conn:
        row = conn.execute(
            "SELECT raw_ref, doc_type, title, period, publish_at, language, source_url,"
            " content_hash, is_correction, source_ref, source"
            " FROM core.document WHERE doc_id = %s",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise click.ClickException(f"doc_id={doc_id} 不存在")
        (
            raw_ref,
            doc_type,
            title,
            period,
            publish_at,
            language,
            source_url,
            content_hash,
            is_correction,
            source_ref,
            source_label,
        ) = row
        if raw_ref is None:
            raise click.ClickException(
                f"doc_id={doc_id} 没有原始文件引用（raw_ref 为空），无法重新切块"
            )

        raw_bytes = blob.get(raw_ref)
        param_fp = param_fingerprint(XPARSE_PARAMS)
        json_key, _ = artifact_keys(TextInParser.content_hash(raw_bytes), param_fp)
        if not blob.exists(json_key):
            raise click.ClickException(
                f"doc_id={doc_id} 没有已归档的解析结果（{json_key} 不存在）——"
                "rechunk 只能复用缓存，不会为了补全它触发新的计费解析"
            )

        # base_url 不会被用到：上面已经确认 json_key 存在，parse() 会走
        # 缓存命中分支直接返回，不发一次网络请求。
        parser = TextInParser("http://rechunk.invalid", blob)
        result = parser.parse(raw_bytes)

        doc = NormalizedDocument(
            provider_doc_id=str(source_ref),
            entity_ref=entity_ref,
            doc_type=str(doc_type),
            title=str(title),
            period=period,
            publish_at=publish_at,
            language=str(language),
            source_url=source_url,
            raw_bytes_ref=raw_ref,
            content_hash=str(content_hash),
            is_correction=bool(is_correction),
            supersedes_provider_doc_id=None,
            page_count=result.page_count,
            blocks=result.blocks,
        )
        chunks, descriptions, violations = build_chunks_and_descriptions(doc, chunk_cfg, describer)
        if violations:
            for violation in violations:
                click.echo(f"  - {violation}", err=True)
            raise click.ClickException(f"doc_id={doc_id} 重新切块未通过校验，见上方违规")

        writer = DocumentWriter(conn, ingest_run_id="cli:docs-rechunk", source=str(source_label))
        artifacts = ParseArtifacts(
            engine=result.engine_version,
            json_ref=result.json_ref,
            md_ref=result.md_ref,
            warnings=list(result.warnings),
        )
        write_result = writer.reparse_document(
            doc, chunks, descriptions, supersedes_doc_id=doc_id, artifacts=artifacts
        )

    click.echo(
        f"重新切块完成：新 doc_id={write_result.doc_id} blocks={len(write_result.block_ids)}"
    )


# --- docs embed --------------------------------------------------------------


@docs_group.command("embed")
@click.option("--limit", type=int, default=None, help="本次最多嵌入多少块；不传则不设上限")
def docs_embed(limit: int | None) -> None:
    """跑一次 `embed_pending_blocks`（`MockEmbedder`）。真实嵌入器留给 Phase 1.4。"""
    with psycopg.connect(_dsn()) as conn:
        stats = embed_pending_blocks(conn, MockEmbedder(), limit=limit)
    click.echo(
        f"pending={stats.pending} from_cache={stats.from_cache} "
        f"computed={stats.computed} written={stats.written}"
    )
