"""评测命令与 CI 门禁比对。

标注入口是 CLI，不是 Web（adr/0006）。adr/0010 只解开了只读诊断界面，
没有解开标注界面，标注工具仍要当正经工具做——否则标注 100 条评测集
会变成瓶颈。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

import click
import psycopg

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest

TOLERANCE = 0.02


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置")
    return dsn


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def compare_to_baseline(
    conn: psycopg.Connection, suite: str, metric: str, current: float, tolerance: float
) -> tuple[bool, float | None]:
    """与最近一次同 suite 的运行比。没有基线时放行——首次运行不该阻断合并。"""
    row = conn.execute(
        "SELECT (metrics ->> %s)::float FROM evals.eval_run "
        " WHERE suite = %s AND metrics ? %s ORDER BY started_at DESC LIMIT 1",
        (metric, suite, metric),
    ).fetchone()
    if row is None or row[0] is None:
        return True, None
    baseline = float(row[0])
    return current >= baseline - tolerance, baseline


@click.group("eval")
def eval_group() -> None:
    """评测集录入与运行。"""


@eval_group.command("add-retrieval")
@click.option("--question", required=True, help="评测问题")
@click.option(
    "--as-of", "as_of_raw", required=True, help="ISO 8601 带时区，如 2024-08-01T00:00+08:00"
)
@click.option("--entity", "entities", multiple=True, help="实体过滤，可多次指定")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option("--author", required=True)
def add_retrieval(
    question: str, as_of_raw: str, entities: tuple[str, ...], difficulty: str, author: str
) -> None:
    """交互式录入一条检索评测用例：跑一次检索，选出正确的块。"""
    as_of = datetime.fromisoformat(as_of_raw)
    if as_of.tzinfo is None:
        raise click.ClickException("--as-of 必须带时区")

    with psycopg.connect(_dsn()) as conn:
        service = RetrievalService(conn, MockEmbedder(), MockReranker())
        result = service.search(
            RetrievalRequest(query=question, as_of=as_of, entity_ids=list(entities) or None)
        )
        if not result.blocks:
            raise click.ClickException("检索没有返回任何候选，无法标注")

        for i, block in enumerate(result.blocks):
            click.echo(f"[{i}] block={block.block_id} p{block.page} {block.doc_title}")
            click.echo(f"     {block.content[:120].replace(chr(10), ' ')}")

        picked = click.prompt("正确的块序号（逗号分隔，可多选）", type=str)
        gold = [result.blocks[int(i.strip())].block_id for i in picked.split(",") if i.strip()]
        if not gold:
            raise click.ClickException("至少要选一个正确答案")

        conn.execute(
            "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids,"
            " entity_filter, difficulty, author) VALUES (%s,%s,%s,%s,%s,%s)",
            (question, as_of, gold, list(entities) or None, difficulty, author),
        )
        conn.commit()
    click.echo(f"已录入，gold_block_ids = {gold}")


@eval_group.command("run")
@click.option("--suite", type=click.Choice(["retrieval"]), default="retrieval")
@click.option("--gate/--no-gate", default=False, help="与基线比对，回退超容差则以非零码退出")
def run(suite: str, gate: bool) -> None:
    """跑评测并写 eval_run。"""
    with psycopg.connect(_dsn()) as conn:
        service = RetrievalService(conn, MockEmbedder(), MockReranker())
        result = run_retrieval_eval(conn, service, git_sha=_git_sha(), config=RetrievalConfig())
        click.echo("| 指标 | 本次 |")
        click.echo("|---|---|")
        click.echo(f"| recall@10 | {result.recall_at_10:.4f} |")
        click.echo(f"| recall@50 | {result.recall_at_50:.4f} |")
        click.echo(f"| MRR@10 | {result.mrr_at_10:.4f} |")
        click.echo(f"| 用例数 | {result.n_cases} |")

        if gate:
            ok, baseline = compare_to_baseline(
                conn, suite, "recall_at_10", result.recall_at_10, TOLERANCE
            )
            if baseline is not None:
                click.echo(f"基线 recall@10 = {baseline:.4f}，容差 {TOLERANCE}")
            if not ok:
                click.echo("回退超出容差，阻断合并", err=True)
                sys.exit(1)
