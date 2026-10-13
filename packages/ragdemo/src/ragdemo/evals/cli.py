"""评测命令与 CI 门禁比对。

标注入口是 CLI，不是 Web（adr/0006）。adr/0010 只解开了只读诊断界面，
没有解开标注界面，标注工具仍要当正经工具做——否则标注 100 条评测集
会变成瓶颈。
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click
import psycopg

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
from ragdemo.evals.table_gate import GateIncomparable, judge, judge_engines
from ragdemo.retrieval.models import build_models
from ragdemo.retrieval.rewrite import load_synonyms
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
@click.option("--models", type=click.Choice(["mock", "siliconflow"]), default="mock")
def add_retrieval(
    question: str,
    as_of_raw: str,
    entities: tuple[str, ...],
    difficulty: str,
    author: str,
    models: str,
) -> None:
    """交互式录入一条检索评测用例：跑一次检索，选出正确的块。"""
    as_of = datetime.fromisoformat(as_of_raw)
    if as_of.tzinfo is None:
        raise click.ClickException("--as-of 必须带时区")

    with psycopg.connect(_dsn()) as conn:
        embedder, reranker = build_models(models)
        service = RetrievalService(conn, embedder, reranker)
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
@click.option(
    "--models",
    type=click.Choice(["mock", "siliconflow"]),
    default="mock",
    show_default=True,
    help="检索用的嵌入 / 重排：mock 或真实（siliconflow）",
)
@click.option("--rerank/--no-rerank", default=True, help="是否启用重排（消融用）")
@click.option(
    "--vector-backend",
    type=click.Choice(["pg", "chroma"]),
    default="pg",
    show_default=True,
    help="向量召回后端：pg = pgvector（对照组）；chroma = 自建 Chroma（ADR-0009）",
)
@click.option("--w-bm25", type=float, default=None, help="BM25 一路的融合权重")
@click.option("--w-vec", type=float, default=None, help="向量一路的融合权重")
@click.option("--candidate-k", type=int, default=None, help="每一路的候选数")
@click.option("--rewrite/--no-rewrite", default=False, help="是否启用同义词查询改写")
@click.option("--period-boost", type=float, default=0.0, help="期别感知排序偏置，0 = 关闭")
@click.option(
    "--bm25-desc/--no-bm25-desc", default=False, help="BM25 同时匹配 content_desc（表格描述）"
)
@click.option(
    "--rerank-rich-input/--no-rerank-rich-input",
    default=False,
    help="重排输入与嵌入同一拼法（标题+章节路径+描述+正文），而非裸正文",
)
@click.option("--label", default="", help="这次运行的备注（写进 eval_run.config，便于事后对比）")
@click.option(
    "--author-prefix",
    default=None,
    help="只跑 author 以此开头的用例：`human:` = 仅人工标注集，`auto:` = 仅合成集",
)
def run(
    suite: str,
    gate: bool,
    models: str,
    rerank: bool,
    vector_backend: str,
    w_bm25: float | None,
    w_vec: float | None,
    candidate_k: int | None,
    rewrite: bool,
    period_boost: float,
    bm25_desc: bool,
    rerank_rich_input: bool,
    label: str,
    author_prefix: str | None,
) -> None:
    """跑评测并写 eval_run（配置与模型模式一并记录）。"""
    embedder, reranker = build_models(models)
    base = RetrievalConfig()
    cand = candidate_k if candidate_k is not None else base.candidate_k
    config = dataclasses.replace(
        base,
        candidate_k=cand,
        fusion_k=min(base.fusion_k, cand * 2),
        w_bm25=w_bm25 if w_bm25 is not None else base.w_bm25,
        w_vec=w_vec if w_vec is not None else base.w_vec,
        rerank_enabled=rerank,
        rewrite_enabled=rewrite,
        period_boost=period_boost,
        bm25_desc=bm25_desc,
        rerank_rich_input=rerank_rich_input,
    )
    config.validate()
    synonyms = load_synonyms() if rewrite else None
    with psycopg.connect(_dsn()) as conn:
        index = None
        if vector_backend == "chroma":
            from ragdemo.retrieval.vector_index import (
                ChromaVectorIndex,
                chroma_client_from_env,
            )

            index = ChromaVectorIndex(chroma_client_from_env())
        service = RetrievalService(conn, embedder, reranker, synonyms=synonyms, index=index)
        # 必须在 run_retrieval_eval 把这次结果写进 evals.eval_run 之前读
        # 基线——见 fetch_baseline 的 docstring，读晚了会把这次自己的结果
        # 当成基线，--gate 形同虚设（H7 集成测试抓到的真实回归）。
        prior_baseline = fetch_baseline(conn, suite, "recall_at_10") if gate else None
        result = run_retrieval_eval(
            conn,
            service,
            git_sha=_git_sha(),
            config=config,
            extra_config={
                "models": models,
                "embed_model": embedder.model,
                "vector_backend": vector_backend,
                "label": label,
                "author_prefix": author_prefix,
            },
            author_prefix=author_prefix,
        )
        click.echo("| 指标 | 本次 |")
        click.echo("|---|---|")
        click.echo(f"| recall@10 | {result.recall_at_10:.4f} |")
        click.echo(f"| recall@50 | {result.recall_at_50:.4f} |")
        click.echo(f"| MRR@10 | {result.mrr_at_10:.4f} |")
        click.echo(f"| p50 延迟 | {result.latency_p50_ms:.0f} ms |")
        click.echo(f"| p95 延迟 | {result.latency_p95_ms:.0f} ms |")
        click.echo(f"| 用例数 | {result.n_cases} |")
        for group, m in result.per_group.items():
            click.echo(
                f"| recall@10（{group}，n={m['n']}）| {float(m['recall_at_10']):.4f} |"
                f" MRR@10 {float(m['mrr_at_10']):.4f} |"
            )

        if gate:
            ok = passes_gate(result.recall_at_10, prior_baseline, TOLERANCE)
            if prior_baseline is not None:
                click.echo(f"基线 recall@10 = {prior_baseline:.4f}，容差 {TOLERANCE}")
            if not ok:
                click.echo("回退超出容差，阻断合并", err=True)
                sys.exit(1)


@eval_group.command("table-gate")
@click.option("--before", "before_id", required=True, help="上一步已接受状态的 eval_run.run_id")
@click.option("--after", "after_id", required=True, help="本次改动的 eval_run.run_id")
@click.option(
    "--by",
    "by",
    type=click.Choice(["case", "engine"]),
    default="case",
    show_default=True,
    help="case = gold 类型 x 出题方式（表格计划）；engine = 解析来源 x 出题方式（T11）",
)
def table_gate(before_id: str, after_id: str, by: str) -> None:
    """按计划写死的表格专项门槛比较两次运行（输出整段贴进 PR）。"""
    with psycopg.connect(_dsn()) as conn:
        rows = {
            str(r[0]): r[1]
            for r in conn.execute(
                "SELECT run_id, metrics FROM evals.eval_run WHERE run_id = ANY(%s)",
                ([before_id, after_id],),
            ).fetchall()
        }
    missing = {before_id, after_id} - set(rows)
    if missing:
        raise click.ClickException(f"找不到运行：{sorted(missing)}")
    b, a = rows[before_id], rows[after_id]
    try:
        judge_fn = judge_engines if by == "engine" else judge
        key = "per_engine" if by == "engine" else "per_group"
        res = judge_fn(
            b.get(key, {}),
            a.get(key, {}),
            overall_before=float(b["recall_at_10"]),
            overall_after=float(a["recall_at_10"]),
        )
    except GateIncomparable as exc:
        raise click.ClickException(f"不可比：{exc}") from exc
    for line in res.report:
        click.echo(line)
    for line in res.reasons:
        click.echo(f"未过：{line}", err=True)
    click.echo("表格门槛：通过" if res.passed else "表格门槛：未过，阻断合并")
    if not res.passed:
        sys.exit(1)


@eval_group.command("sample")
@click.option("--n", "n", type=int, default=100, help="候选总数（各分层保底之后再补齐到这个数）")
@click.option("--seed", type=int, default=DEFAULT_SEED, help="固定种子，保证同一种子两次抽中同一批")
@click.option("--min-per-stratum", type=int, default=DEFAULT_MIN_PER_STRATUM)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    required=True,
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
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    required=True,
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


@eval_group.command("generate")
@click.option("--n", "n", type=int, default=100, show_default=True, help="生成多少条检索用例")
@click.option("--seed", type=int, default=DEFAULT_SEED, show_default=True)
@click.option(
    "--replace-auto",
    is_flag=True,
    default=False,
    help="先删除此前生成的合成用例（author 以 auto: 开头），人工用例不动",
)
@click.option("--yes", is_flag=True, default=False, help="真正调用模型并写库；不传只 dry run")
def generate(n: int, seed: int, replace_auto: bool, yes: bool) -> None:
    """据真实语料合成检索评测用例（模型写问题、分层抽样选块、同文块并列为 gold）。
    author 记为 auto:<模型>:v1，notes 记生成参数——合成集与人工标注集始终可区分。"""
    from datetime import UTC

    from ragdemo.config import load_config
    from ragdemo.evals.generate import PROMPT_VERSION, generate_cases, insert_cases
    from ragdemo.parse.describer_factory import deepseek_client

    cfg = load_config()
    with psycopg.connect(_dsn()) as conn:
        existing = conn.execute(
            "SELECT count(*) FILTER (WHERE author LIKE 'auto:%'), count(*)"
            " FROM evals.eval_retrieval"
        ).fetchone()
        assert existing is not None
        auto_n, total_n = int(existing[0]), int(existing[1])
        click.echo(f"当前评测集 {total_n} 条，其中合成 {auto_n} 条")
        if auto_n and not replace_auto:
            raise click.ClickException("已有合成用例；要重新生成请加 --replace-auto")
        if not yes:
            click.echo(f"[dry run] 将生成 {n} 条（约 {n} 次模型调用）；加 --yes 执行")
            return
        if not cfg.model_base_url:
            raise click.ClickException("MODEL_BASE_URL 未设置：生成用例需要真实模型")
        http = deepseek_client(cfg.model_base_url)
        cases, report = generate_cases(conn, http, model=cfg.model_name, n=n, seed=seed)
        if replace_auto:
            conn.execute("DELETE FROM evals.eval_retrieval WHERE author LIKE 'auto:%'")
        inserted = insert_cases(
            conn,
            cases,
            as_of=datetime.now(UTC),
            author=f"auto:{cfg.model_name}:{PROMPT_VERSION}",
        )
    by_difficulty: dict[str, int] = {}
    for c in cases:
        by_difficulty[c.difficulty] = by_difficulty.get(c.difficulty, 0) + 1
    click.echo(f"生成并入库 {inserted} 条；难度（按实测词面重合度）{by_difficulty}")
    if report.skipped:
        click.echo(f"跳过的候选：{report.skipped}")
    if inserted < n:
        click.echo(f"[警告] 只凑到 {inserted} 条（目标 {n}）", err=True)


@eval_group.command("label-sheet")
@click.option("--out", "out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--n", "n", type=int, default=120, show_default=True, help="导出多少个候选段落")
@click.option("--seed", type=int, default=DEFAULT_SEED, show_default=True)
@click.option(
    "--parse-source",
    type=click.Choice(["local", "textin", "legacy"]),
    default=None,
    help="只导出某一解析来源的段落（L0 页 / TextIn 页 / 存量）",
)
def label_sheet(out: Path, n: int, seed: int, parse_source: str | None) -> None:
    """导出人工标注表（段落 + 空的 question 列）；默认排除已有用例用过的源块。"""
    from ragdemo.evals.human_labels import export_label_sheet

    with psycopg.connect(_dsn()) as conn:
        count = export_label_sheet(conn, out, n=n, seed=seed, parse_source=parse_source)
    click.echo(f"已导出 {count} 个段落到 {out}；填好 question / difficulty 后用 label-import 导入")


@eval_group.command("table-label-sheet")
@click.option("--out", "out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--seed", type=int, default=20261013, show_default=True)
def table_label_sheet(out: Path, seed: int) -> None:
    """导出人工表格题标注表（四层按配额 2 倍候选；passage 不含表格描述）。"""
    from ragdemo.evals.table_labels import TABLE_QUOTA, export_table_label_sheet

    with psycopg.connect(_dsn()) as conn:
        count = export_table_label_sheet(conn, out, seed=seed)
    quota = "、".join(f"{k} {v}" for k, v in TABLE_QUOTA.items())
    click.echo(f"已导出 {count} 个候选表格到 {out}（配额：{quota}）；填好后用 label-import 导入")


@eval_group.command("label-import")
@click.option(
    "--file", "path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--annotator", required=True, help="标注员名字，记为 author = human:<名字>")
@click.option("--yes", is_flag=True, default=False, help="真正写库；不传只校验")
def label_import(path: Path, annotator: str, yes: bool) -> None:
    """校验并导入人工标注表；有任何问题整批不写。"""
    from datetime import UTC

    from ragdemo.evals.human_labels import LabelSheetError, insert_labeled, parse_label_sheet

    with psycopg.connect(_dsn()) as conn:
        try:
            cases = parse_label_sheet(conn, path)
        except LabelSheetError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"校验通过：{len(cases)} 条")
        if not yes:
            click.echo("[dry run] 未写库；加 --yes 写入")
            return
        n = insert_labeled(conn, cases, as_of=datetime.now(UTC), annotator=annotator)
    click.echo(f"已写入 {n} 条（author = human:{annotator}）")
