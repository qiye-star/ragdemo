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
from pathlib import Path

import click
import psycopg

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.freeze import (
    create_freeze,
    diff_against_current_corpus,
    load_freeze,
    save_freeze,
)
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.evals.sampler import (
    DEFAULT_MIN_PER_STRATUM,
    DEFAULT_SEED,
    sample_candidates,
    write_candidates_csv,
)
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


def fetch_baseline(conn: psycopg.Connection, suite: str, metric: str) -> float | None:
    """最近一次同 suite 的运行记的这个指标。None 表示还没有任何历史记录。

    **调用时机很关键**：必须在这次要评估的运行把自己的结果写进
    `evals.eval_run` 之前调用——`run_retrieval_eval` 会在计算完指标后
    立刻把这次结果记一行进这张表，读晚了，"最近一次"就是这次自己，
    门禁变成永远和自己比、永远放行（阶段 H 写 H7 集成测试时抓到的真实
    回归：`eval run --gate` 因为这个调用顺序问题从未真正拦截过任何回退，
    见 `run()` 里的调用顺序）。
    """
    row = conn.execute(
        "SELECT (metrics ->> %s)::float FROM evals.eval_run "
        " WHERE suite = %s AND metrics ? %s ORDER BY started_at DESC LIMIT 1",
        (metric, suite, metric),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def passes_gate(current: float, baseline: float | None, tolerance: float) -> bool:
    """没有基线时放行——首次运行不该阻断合并。"""
    return baseline is None or current >= baseline - tolerance


def compare_to_baseline(
    conn: psycopg.Connection, suite: str, metric: str, current: float, tolerance: float
) -> tuple[bool, float | None]:
    """`fetch_baseline` + `passes_gate` 的组合，供直接传入外部 `current`
    值的调用方使用（如单测——先造好一行历史基线，再拿一个单独算出来的
    `current` 来比，`current` 本身从未被写进 `evals.eval_run`，不存在
    "读到自己" 的问题）。`eval run --gate` 不走这个函数：它必须先读
    `fetch_baseline`，再调 `run_retrieval_eval`（那一步会写入这次结果），
    两步顺序不能颠倒，见 `fetch_baseline` 的 docstring。
    """
    baseline = fetch_baseline(conn, suite, metric)
    return passes_gate(current, baseline, tolerance), baseline


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
        # 必须在 run_retrieval_eval 把这次结果写进 evals.eval_run 之前读
        # 基线——见 fetch_baseline 的 docstring，读晚了会把这次自己的结果
        # 当成基线，--gate 形同虚设（H7 集成测试抓到的真实回归）。
        prior_baseline = fetch_baseline(conn, suite, "recall_at_10") if gate else None
        result = run_retrieval_eval(conn, service, git_sha=_git_sha(), config=RetrievalConfig())
        click.echo("| 指标 | 本次 |")
        click.echo("|---|---|")
        click.echo(f"| recall@10 | {result.recall_at_10:.4f} |")
        click.echo(f"| recall@50 | {result.recall_at_50:.4f} |")
        click.echo(f"| MRR@10 | {result.mrr_at_10:.4f} |")
        click.echo(f"| 用例数 | {result.n_cases} |")

        if gate:
            ok = passes_gate(result.recall_at_10, prior_baseline, TOLERANCE)
            if prior_baseline is not None:
                click.echo(f"基线 recall@10 = {prior_baseline:.4f}，容差 {TOLERANCE}")
            if not ok:
                click.echo("回退超出容差，阻断合并", err=True)
                sys.exit(1)


@eval_group.command("sample")
@click.option("--n", "n", type=int, default=100, help="候选总数（各分层保底之后再补齐到这个数）")
@click.option("--seed", type=int, default=DEFAULT_SEED, help="固定种子，保证同一种子两次抽中同一批")
@click.option("--min-per-stratum", type=int, default=DEFAULT_MIN_PER_STRATUM)
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), required=True,
    help="输出 CSV 路径，如 evals/retrieval/candidates.csv",
)
def sample(n: int, seed: int, min_per_stratum: int, out_path: Path) -> None:
    """按 doc_type × parse_engine 族 × entity_id 分层抽取候选块，供创始人
    据此写评测题、挑 gold_block_ids（阶段 H：标注本身不在这个命令的范围内）。
    """
    with psycopg.connect(_dsn()) as conn:
        candidates = sample_candidates(conn, n=n, seed=seed, min_per_stratum=min_per_stratum)
    if not candidates:
        raise click.ClickException("候选池为空——core.doc_block 里没有任何活着的叶子块")
    write_candidates_csv(out_path, candidates)
    strata = {(c.doc_type, c.parse_engine.split(":", 1)[0], c.entity_id or "") for c in candidates}
    click.echo(f"抽出 {len(candidates)} 条候选（{len(strata)} 个分层），已写入 {out_path}")


@eval_group.command("freeze")
@click.option(
    "--as-of", "as_of_raw", required=True, help="ISO 8601 带时区，如 2024-08-01T00:00+08:00"
)
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), required=True,
    help="快照输出路径，如 evals/retrieval/2026-10-freeze.json",
)
def freeze(as_of_raw: str, out_path: Path) -> None:
    """冻结这个 as_of 下的语料快照——block_id 集合 + chunking_version /
    embedding_version，供之后判断"分数变化是检索改好了还是语料变了"。"""
    as_of = datetime.fromisoformat(as_of_raw)
    if as_of.tzinfo is None:
        raise click.ClickException("--as-of 必须带时区")

    with psycopg.connect(_dsn()) as conn:
        snapshot = create_freeze(conn, as_of)
    save_freeze(snapshot, out_path)
    click.echo(f"冻结 {len(snapshot.block_ids)} 个块，已写入 {out_path}")


@eval_group.command("freeze-diff")
@click.option(
    "--freeze-file", "freeze_path", type=click.Path(exists=True, path_type=Path), required=True
)
def freeze_diff(freeze_path: Path) -> None:
    """把已有快照与当前语料库重放比对——不一致就说明语料变了，评测分数的
    变化不能全部归因于检索改动本身。"""
    snapshot = load_freeze(freeze_path)
    with psycopg.connect(_dsn()) as conn:
        diffs = diff_against_current_corpus(conn, snapshot)
    if not diffs:
        click.echo("语料库与冻结快照一致")
        return
    for d in diffs:
        click.echo(f"[不一致] {d}")
    sys.exit(1)
