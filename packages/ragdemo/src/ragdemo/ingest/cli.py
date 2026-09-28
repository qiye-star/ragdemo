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
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import click
import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.cninfo import CninfoAdapter
from ragdemo.adapters.edgar import TRACKED_FORMS, EdgarAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.local_manifest import (
    ManifestError,
    count_pdf_pages,
    load_documents_with_raw,
)
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.adapters.wind_docs import WindDocsAdapter
from ragdemo.config import ConfigError, load_config
from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo.ingest.assets_docs import (
    build_chunks_and_descriptions,
    prepare_documents,
    record_parse_snapshots,
)
from ragdemo.ingest.cninfo_docs import ingest_cninfo_announcements, list_announcements
from ragdemo.ingest.documents import DocumentWriter, MetadataInvalid, ParseArtifacts
from ragdemo.ingest.edgar_docs import ingest_edgar_filings, list_filings
from ragdemo.ingest.parse_pending import run_parse_pending
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber, TableDescriber
from ragdemo.parse.textin import (
    XPARSE_PARAMS,
    DocumentParser,
    MockDocumentParser,
    PageBudget,
    TextInParser,
    artifact_keys,
    param_fingerprint,
)
from ragdemo.quality.announcement_coverage import check_announcement_coverage
from ragdemo.quality.budget import BudgetExceeded, ensure_budget
from ragdemo_core.blob import BlobStore, LocalBlobStore


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
        pairs = load_documents_with_raw(
            manifest_path, blob, entity_ref=entity_ref, select_title=select_title
        )
    except ManifestError as exc:
        raise click.ClickException(str(exc)) from exc

    docs = [doc for _, doc in pairs]
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
        if parser_kind == "textin":
            try:
                budget_status = ensure_budget(
                    conn, now=datetime.now(UTC), cap_cny=cfg.monthly_budget_cny
                )
            except BudgetExceeded as exc:
                raise click.ClickException(str(exc)) from None
            click.echo(f"[预算] {budget_status.describe()}")
        writer = DocumentWriter(conn, ingest_run_id="cli:docs-ingest", source=source_name)
        # 每条 manifest 记录先落 provider_snapshot 再解析（CLAUDE.md §1.5）；
        # dry run 不走到这里，不写库。
        docs = [
            replace(
                doc,
                snapshot_id=record_snapshot(conn, raw, ingest_run_id="cli:docs-ingest", blob=blob),
            )
            for raw, doc in pairs
        ]
        prepared = prepare_documents(docs, parser, blob, budget, owner_user=None)
        record_parse_snapshots(
            conn,
            prepared,
            ingest_run_id="cli:docs-ingest",
            cost_per_page_cny=cfg.textin_cost_per_page_cny,
        )
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
                    chunking_version=chunk_cfg.version,
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
            " content_hash, is_correction, source_ref, source, snapshot_id"
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
            snapshot_id,
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
            # 重切不产生新的原始响应，沿用原文档的快照。
            snapshot_id=snapshot_id,
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
            doc,
            chunks,
            descriptions,
            supersedes_doc_id=doc_id,
            artifacts=artifacts,
            chunking_version=chunk_cfg.version,
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


# --- docs ingest-edgar（阶段 F：F5，全文抓取此前完全没有代码）----------------


def _edgar_user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise click.ClickException(
            "环境变量 EDGAR_USER_AGENT 未设置。SEC 的公平访问政策要求每个请求带"
            '能联系到人的 User-Agent（形如 "组织名 联系邮箱"），这个值属于部署'
            "配置（谁在运营这次抓取），不该由代码编造一个假联系方式。"
        )
    return ua


@docs_group.command("ingest-edgar")
@click.option("--cik", required=True, type=int, help="SEC 中央索引码，如 1045810（NVIDIA）")
@click.option("--source", default="edgar", help="写入 core.document.source 的值")
@click.option("--yes", is_flag=True, default=False, help="真正抓取全文并写库；不传则只 dry run")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="本次最多抓取几份申报的全文；不传则处理命中的全部申报（一家公司可能有"
    "几十上百份历史申报，回填全部历史时应显式设置这个值，避免一次性发出"
    "大量请求）",
)
@click.option("--blob-root", "blob_root_opt", type=click.Path(path_type=Path), default=None)
def docs_ingest_edgar(
    cik: int, source: str, yes: bool, limit: int | None, blob_root_opt: Path | None
) -> None:
    """按 CIK 拉 EDGAR 申报列表，取全文字节存 blob，写一行 core.document
    （路径 B，未解析——本命令只证明「原始文档真的能取到、真的能存住」，
    这两步之后的解析走既有的 `docs ingest`/`docs rechunk` 流程，不在这里
    重新实现一遍）。

    默认 dry run：只列出命中的申报，不发起任何全文抓取（EDGAR 的全文体积
    可能有几百 KB 到几 MB，不该在没有 --yes 的情况下悄悄发出去）。
    """
    user_agent = _edgar_user_agent()
    submissions_client = HttpClient(
        provider="edgar",
        base_url="https://data.sec.gov",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=1.0),
        bucket=TokenBucket(rate_per_minute=60),
        default_headers={"User-Agent": user_agent},
    )
    adapter = EdgarAdapter(client=submissions_client)

    if not yes:
        refs = list_filings(adapter, cik=cik, ingest_run_id="cli:docs-ingest-edgar")
        click.echo(f"[dry run] CIK {cik} 命中 {len(refs)} 份申报（{sorted(TRACKED_FORMS)}）")
        for ref in refs:
            click.echo(f"  - {ref.form_type} {ref.accession} {ref.document_url}")
        click.echo("[dry run] 未发起任何全文抓取，--yes 才会真正取全文并写库")
        return

    cfg = _load_config()
    blob = LocalBlobStore(blob_root_opt or cfg.blob_root)
    archives_client = HttpClient(
        provider="edgar-archives",
        base_url="https://www.sec.gov",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=1.0),
        bucket=TokenBucket(rate_per_minute=60),
        default_headers={"User-Agent": user_agent},
    )

    with psycopg.connect(_dsn()) as conn:
        results = ingest_edgar_filings(
            conn,
            adapter=adapter,
            archives_client=archives_client,
            blob=blob,
            cik=cik,
            source=source,
            limit=limit,
            ingest_run_id="cli:docs-ingest-edgar",
        )
        for ref, result in results:
            status = "跳过(已存在)" if result.skipped else "写入"
            click.echo(f"{status} doc_id={result.doc_id} {ref.form_type} {ref.accession}")


# --- docs ingest-cninfo（adr/0013：公告全文主源改巨潮资讯网直连）------------


def _cninfo_user_agent() -> str:
    ua = os.environ.get("RAGDEMO_CNINFO_USER_AGENT", "").strip()
    if not ua:
        raise click.ClickException(
            "环境变量 RAGDEMO_CNINFO_USER_AGENT 未设置。访问巨潮资讯网要带能联系到人的"
            'User-Agent（形如 "组织名 联系邮箱"，与 EDGAR_USER_AGENT 同一个理由），'
            "不该伪装成浏览器，这个值属于部署配置，不该由代码编造。"
        )
    return ua


@docs_group.command("ingest-cninfo")
@click.option("--stock", required=True, help="巨潮的股票代码，如 688041（不带 .SH 后缀）")
@click.option(
    "--entity-ref",
    required=True,
    help="显式实体代码，如 688041.SH，匹配 core.entity.tushare_code——与 docs ingest"
    "同一个理由：不从股票代码猜 entity_ref，避免静默写成 entity_id=NULL",
)
@click.option("--since", required=True, help="起始日期 YYYY-MM-DD")
@click.option("--until", required=True, help="截止日期 YYYY-MM-DD（含）")
@click.option("--source", default="cninfo", help="写入 core.document.source 的值")
@click.option("--yes", is_flag=True, default=False, help="真正下载 PDF 并写库；不传则只 dry run")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="本次最多入库几份公告；不传则处理命中的全部公告（一家公司在几年窗口内"
    "可能有几十上百份公告，回填历史时应显式设置这个值，避免一次性发出大量请求——"
    "adr/0013 提醒过巨潮的频控没有官方保证）",
)
@click.option("--blob-root", "blob_root_opt", type=click.Path(path_type=Path), default=None)
def docs_ingest_cninfo(
    stock: str,
    entity_ref: str,
    since: str,
    until: str,
    source: str,
    yes: bool,
    limit: int | None,
    blob_root_opt: Path | None,
) -> None:
    """按股票代码拉巨潮资讯网公告列表，取 PDF 字节存 blob，写一行 core.document
    （路径 B，未解析——本命令只证明「原始公告真的能取到、真的能存住」，这两步
    之后的解析走既有的 `docs ingest`/`docs rechunk` 流程，不在这里重新实现一遍）。

    默认 dry run：只列出命中的公告，不发起任何 PDF 下载（不该在没有 --yes 的
    情况下悄悄对巨潮发出批量请求）。
    """
    user_agent = _cninfo_user_agent()
    since_dt = datetime.combine(date.fromisoformat(since), time.min, tzinfo=UTC)
    until_dt = datetime.combine(date.fromisoformat(until), time.max, tzinfo=UTC)
    query_client = HttpClient(
        provider="cninfo",
        base_url="https://www.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=1.0),
        # 60/分钟（1 次/秒）是保守估计，不是巨潮承诺的 SLA——adr/0013
        # "后果" 一节已经写明，真实批量抓取时按观察结果调整。
        bucket=TokenBucket(rate_per_minute=60),
        default_headers={"User-Agent": user_agent},
    )
    adapter = CninfoAdapter(query_client)

    if not yes:
        refs = list_announcements(
            adapter,
            stock_code=stock,
            since=since_dt,
            until=until_dt,
            ingest_run_id="cli:docs-ingest-cninfo",
        )
        click.echo(f"[dry run] {stock} 命中 {len(refs)} 份带 PDF 附件的公告")
        for ref in refs:
            click.echo(f"  - {ref.publish_at.date()} {ref.title} {ref.pdf_path}")
        click.echo("[dry run] 未发起任何 PDF 下载，--yes 才会真正下载并写库")
        return

    cfg = _load_config()
    blob = LocalBlobStore(blob_root_opt or cfg.blob_root)
    static_client = HttpClient(
        provider="cninfo-static",
        base_url="https://static.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=1.0),
        bucket=TokenBucket(rate_per_minute=60),
        default_headers={"User-Agent": user_agent},
    )

    with psycopg.connect(_dsn()) as conn:
        results = ingest_cninfo_announcements(
            conn,
            adapter=adapter,
            static_client=static_client,
            blob=blob,
            stock_code=stock,
            entity_ref=entity_ref,
            source=source,
            since=since_dt,
            until=until_dt,
            limit=limit,
            ingest_run_id="cli:docs-ingest-cninfo",
        )
        for ref, result in results:
            status = "跳过(已存在)" if result.skipped else "写入"
            click.echo(f"{status} doc_id={result.doc_id} {ref.publish_at.date()} {ref.title}")


# --- docs parse-pending（公告双源接入计划 Task 10）---------------------------


@docs_group.command("parse-pending")
@click.option(
    "--entity-ref",
    default=None,
    help="只处理这一家实体（core.entity 任一代码列，如 688041.SH / 1045810）；"
    "不传则处理全部实体——"
    "扩大解析范围到更多试点时先用这个参数控制影响面。",
)
@click.option(
    "--tier",
    "tier_names",
    multiple=True,
    type=click.Choice([t.value for t in ParseTier]),
    default=("first",),
    help="只解析这些档位（可重复传多次）；不传则只处理 ParseTier.FIRST。",
)
@click.option("--max-pages", "max_pages_opt", type=int, default=None, help="本次 run 的页数预算")
@click.option("--yes", is_flag=True, default=False, help="真正调用解析器并写库；不传则只 dry run")
@click.option(
    "--parser",
    "parser_kind",
    type=click.Choice(["textin", "mock"]),
    default="textin",
    help="mock 用于离线开发与测试，完全不发网络请求",
)
@click.option("--blob-root", "blob_root_opt", type=click.Path(path_type=Path), default=None)
def docs_parse_pending(
    entity_ref: str | None,
    tier_names: tuple[str, ...],
    max_pages_opt: int | None,
    yes: bool,
    parser_kind: str,
    blob_root_opt: Path | None,
) -> None:
    """解析已经在库里、有原文却还没有块的文档（cninfo/hkexnews/edgar 三个来源）。

    不重新抓取任何东西——`docs announcements` 只对试点实体下载全文，且默认只请求
    `ParseTier.FIRST` 档；决定要给某个实体补全文解析、或者扩大解析范围到更多档位/
    更多实体时，跑这个命令。默认 dry run：只报选中了多少份、按档位汇总的真实页数
    （能读到 PDF 时）或经验估算（读不到时），不发起任何解析调用。
    """
    cfg = _load_config()
    blob = LocalBlobStore(blob_root_opt or cfg.blob_root)
    tiers = frozenset(ParseTier(name) for name in tier_names)
    max_pages = max_pages_opt if max_pages_opt is not None else cfg.textin_max_pages_per_run

    with psycopg.connect(_dsn()) as conn:
        if not yes:
            report = run_parse_pending(
                conn,
                parser=MockDocumentParser(),
                blob=blob,
                entity_ref=entity_ref,
                tiers=tiers,
                budget=PageBudget(max_pages),
                ingest_run_id="cli:docs-parse-pending",
                dry_run=True,
            )
            click.echo(f"[dry run] 选中 {report.selected} 份待解析文档")
            for tier, pages in sorted(report.estimated_pages.items()):
                click.echo(f"  - {tier.value}: {pages} 页")
            click.echo("[dry run] 未发起任何解析调用，--yes 才会真正解析并写库")
            return

        parser: DocumentParser
        if parser_kind == "mock":
            parser = MockDocumentParser()
        else:
            base_url = cfg.require_textin_base_url()
            parser = TextInParser(base_url, blob, allow_private=cfg.textin_allow_private)

        if parser_kind == "textin":
            try:
                budget_status = ensure_budget(
                    conn, now=datetime.now(UTC), cap_cny=cfg.monthly_budget_cny
                )
            except BudgetExceeded as exc:
                raise click.ClickException(str(exc)) from None
            click.echo(f"[预算] {budget_status.describe()}")

        report = run_parse_pending(
            conn,
            parser=parser,
            blob=blob,
            entity_ref=entity_ref,
            tiers=tiers,
            budget=PageBudget(max_pages),
            ingest_run_id="cli:docs-parse-pending",
            dry_run=False,
        )
        click.echo(
            f"选中 {report.selected}，重解析 {report.reparsed}"
            f"（{report.parsed_pages} 页），无块 {report.stored_unparsed}，"
            f"预算跳过 {report.skipped_budget}"
        )


# --- docs check-coverage（公告双源接入计划 Task 11）--------------------------


@docs_group.command("check-coverage")
@click.option("--since", required=True, help="起始日期 YYYY-MM-DD")
@click.option("--until", required=True, help="截止日期 YYYY-MM-DD（含）")
def docs_check_coverage(since: str, until: str) -> None:
    """A 股公告漏收核对：按月问 Wind，与巨潮目录比对，把疑似漏收清单写进
    `quality.quality_metric`（`announcement_coverage_gap`）。不写目录——这只是
    一次核对，不是又一条数据入库路径。港股/美股走官方渠道，不在这个核对范围内。
    """
    since_d = date.fromisoformat(since)
    until_d = date.fromisoformat(until)
    blob = LocalBlobStore(_load_config().blob_root)
    gateway = gateway_client_from_env(provider="wind")
    wind = WindDocsAdapter(gateway)

    with psycopg.connect(_dsn()) as conn:
        gaps = check_announcement_coverage(
            conn,
            wind=wind,
            blob=blob,
            since=since_d,
            until=until_d,
            partition_date=until_d,
            ingest_run_id="cli:docs-check-coverage",
        )

    if not gaps:
        click.echo("未发现疑似漏收")
        return
    click.echo(f"疑似漏收 {len(gaps)} 份：")
    for gap in gaps:
        click.echo(f"  - {gap.entity_ref} {gap.publish_date} {gap.title}")
