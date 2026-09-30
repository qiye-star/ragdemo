"""日度调度与告警传感器（阶段 4.2 / 4.3）。

本文件刻意不用 `from __future__ import annotations`：Dagster 在 `@op` / `@asset` 装饰期对
`context` 参数做类型校验时直接比较 `Parameter.annotation`，不解析 PEP 563 的延迟字符串注解
（与 `assets.py` / `definitions.py` 同一个原因）。

调度一览（北京时间；行情、公告都是**增量**——只有新数据才触发处理，不做全量重算，
CLAUDE.md §1.6）：

- `prices_job`：工作日 17:00，当日行情（Tushare MCP → REST → Wind K 线三层降级），免费；
- `announcements_job`：每天 20:30，公告目录增量（巨潮 / 披露易 / EDGAR），只对试点实体
  下载并解析，解析计费受页数预算限制；
- `stage2_verify_job`：每天 21:30，阶段二数据体检（股东 / 分部 / 汇率 / 财务 `known_at`
  互证），出错告警，只读库；
- `leak_replay_job`：每天 03:30，时点泄漏抽样重放（200 块 × 3 个历史 as_of），只读库；
- `quality_alerts_job`：每天 22:00，当天未通过的质量指标与预算状态 → 告警；
- `announcement_reconcile_job`：周日 23:00，公告总数对账（来源自报总数 vs 库里行数），
  不一致告警，只读来源（免费）；
- `alert_selftest_job`：只手动触发，故意失败一次，验证失败告警通路；
- `wind_facts_job`：**默认关闭**，Wind 财务增量（已有财务数据的实体），**耗 Wind 额度**，
  需人工在 Dagster 里打开。

任何作业失败由 `alert_on_run_failure` 传感器推送告警（`ops/alerts.py`）。
"""

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import psycopg
from dagster import (
    AssetSelection,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    JobDefinition,
    OpExecutionContext,
    ResourceParam,
    RunFailureSensorContext,
    RunRequest,
    ScheduleDefinition,
    ScheduleEvaluationContext,
    define_asset_job,
    job,
    op,
    run_failure_sensor,
    schedule,
)

from ragdemo.config import load_config
from ragdemo.ingest.announcement_verify import verify_announcements
from ragdemo.ingest.announcements import AnnouncementSources, entity_routes
from ragdemo.ingest.assets import DAILY, price_daily_loaded, price_normalized
from ragdemo.ingest.stage2_verify import verify_stage2
from ragdemo.ops.alerts import Alert, raise_alert
from ragdemo.ops.quality_alerts import evaluate_quality_alerts
from ragdemo.quality.replay import replay_and_check

if TYPE_CHECKING:  # 顶层没有导出这个类型，只在类型检查时导入
    from dagster._core.definitions.unresolved_asset_job_definition import (
        UnresolvedAssetJobDefinition,
    )

CST = timezone(timedelta(hours=8))


def _today() -> str:
    return datetime.now(CST).date().isoformat()


# --- 资产型作业：行情、公告 ------------------------------------------------------------

prices_job = define_asset_job(
    "prices_job",
    selection=AssetSelection.assets(price_normalized, price_daily_loaded),
    partitions_def=DAILY,
)
announcements_job = define_asset_job(
    "announcements_job",
    # 按资产键名选：`announcements_daily` 定义在 definitions.py，直接 import 会循环依赖。
    selection=AssetSelection.assets("announcements_daily"),
    partitions_def=DAILY,
)


@schedule(
    job=prices_job,
    cron_schedule="0 17 * * 1-5",
    execution_timezone="Asia/Shanghai",
    default_status=DefaultScheduleStatus.RUNNING,
)
def prices_schedule(context: ScheduleEvaluationContext) -> RunRequest:
    """收盘后取当天行情；分区键是今天（DAILY 的分区按北京时间日期）。"""
    return RunRequest(partition_key=_today(), run_key=f"prices-{_today()}")


@schedule(
    job=announcements_job,
    cron_schedule="30 20 * * *",
    execution_timezone="Asia/Shanghai",
    default_status=DefaultScheduleStatus.RUNNING,
)
def announcements_schedule(context: ScheduleEvaluationContext) -> RunRequest:
    """公告晚间集中披露，晚 8:30 取当天增量。"""
    return RunRequest(partition_key=_today(), run_key=f"announcements-{_today()}")


# --- 运维型作业：体检、重放、告警、对账 ------------------------------------------------


@op
def stage2_verify_op(context: OpExecutionContext, conn: ResourceParam[psycopg.Connection]) -> int:
    findings = verify_stage2(conn, as_of=datetime.now(UTC))
    errors = [f for f in findings if f.severity == "error"]
    for f in errors:
        raise_alert(
            conn,
            Alert(
                kind="quality",
                severity="critical",
                key=f"stage2:{f.check}:{f.message[:80]}",
                title=f"阶段二数据体检出错：{f.check}",
                detail=f.message,
            ),
        )
    context.log.info(f"stage2 verify: {len(errors)} errors, {len(findings) - len(errors)} warnings")
    return len(errors)


@op
def leak_replay_op(context: OpExecutionContext, conn: ResourceParam[psycopg.Connection]) -> int:
    mismatches = replay_and_check(conn, datetime.now(CST).date())
    if mismatches:
        first = mismatches[0]
        raise_alert(
            conn,
            Alert(
                kind="quality",
                severity="critical",
                key=f"leak-replay:{datetime.now(CST).date().isoformat()}",
                title=f"时点泄漏抽样重放发现 {len(mismatches)} 处不一致",
                detail=f"例：block_id={first.block_id} as_of={first.as_of.isoformat()}",
            ),
        )
    context.log.info(f"leak replay mismatches: {len(mismatches)}")
    return len(mismatches)


@op
def quality_alerts_op(context: OpExecutionContext, conn: ResourceParam[psycopg.Connection]) -> int:
    cfg = load_config()
    today = datetime.now(CST).date()
    alerts = evaluate_quality_alerts(
        conn, today, cap_cny=cfg.monthly_budget_cny, now=datetime.now(UTC)
    )
    for alert in alerts:
        raise_alert(conn, alert)
    context.log.info(f"quality alerts raised: {len(alerts)}")
    return len(alerts)


@op
def announcement_reconcile_op(
    context: OpExecutionContext,
    conn: ResourceParam[psycopg.Connection],
    announcement_sources: ResourceParam[AnnouncementSources],
) -> int:
    today = datetime.now(CST).date()
    rows = verify_announcements(
        conn,
        entity_routes(conn),
        announcement_sources,
        since=today.replace(year=today.year - 3),
        until=today,
    )
    bad = [r for r in rows if not r.ok]
    if bad:
        raise_alert(
            conn,
            Alert(
                kind="reconcile",
                severity="critical",
                key=f"announcement-reconcile:{today.isocalendar().year}-W{today.isocalendar().week}",
                title=f"公告对账不平：{len(bad)}/{len(rows)} 家实体来源总数与库里不一致",
                detail="；".join(f"{r.entity_ref}: {r.note}" for r in bad[:10]),
            ),
        )
    context.log.info(f"announcement reconcile: {len(rows)} checked, {len(bad)} mismatched")
    return len(bad)


@op
def wind_facts_op(context: OpExecutionContext, conn: ResourceParam[psycopg.Connection]) -> int:
    """Wind 财务增量：只对已有财务数据的实体取最近两个季度（迟到的年报也能在下次补上）。
    **耗 Wind 额度**（每实体 1 次调用），调度默认关闭，需要人工在 Dagster 里打开。"""
    import os

    from ragdemo.adapters.mcp_gateway import gateway_client_from_env
    from ragdemo.adapters.wind import WindFundamentalsAdapter
    from ragdemo.ingest.wind_facts import ingest_wind_facts
    from ragdemo.sources.backlog import Backlog
    from ragdemo.sources.health import PgSourceHealth
    from ragdemo_core.blob import LocalBlobStore

    codes = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT e.tushare_code FROM core.entity e JOIN core.fin_fact f"
            " ON f.entity_id = e.entity_id WHERE e.tushare_code IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    gateway = gateway_client_from_env(url_env="RAGDEMO_WIND_MCP_URL", provider="wind-mcp")
    adapter = WindFundamentalsAdapter(gateway, windcodes=codes, quarters=2)
    with psycopg.connect(os.environ["RAGDEMO_DSN"], autocommit=True) as state:
        report = ingest_wind_facts(
            conn,
            adapter,
            LocalBlobStore(load_config().blob_root),
            windcodes=codes,
            partition_date=datetime.now(CST).date(),
            ingest_run_id=f"dagster:wind-facts:{_today()}",
            quarters=2,
            health=PgSourceHealth(state),
            backlog=Backlog(state),
        )
    context.log.info(f"wind facts: written={report.written} skipped={report.skipped}")
    return report.written


@op
def alert_selftest_op() -> None:
    """告警通路自检：故意失败一次，验证"作业失败 → 传感器 → 告警渠道"整条链是通的
    （总计划 4.3 的验收：人为制造一次失败能收到告警）。只手动触发，没有调度。"""
    raise RuntimeError("告警自检：这是一次人为制造的失败，收到告警说明通路正常")


@job
def alert_selftest_job() -> None:
    alert_selftest_op()


@job
def stage2_verify_job() -> None:
    stage2_verify_op()


@job
def leak_replay_job() -> None:
    leak_replay_op()


@job
def quality_alerts_job() -> None:
    quality_alerts_op()


@job
def announcement_reconcile_job() -> None:
    announcement_reconcile_op()


@job
def wind_facts_job() -> None:
    wind_facts_op()


def _cron(
    job_def: JobDefinition, cron: str, name: str, *, running: bool = True
) -> ScheduleDefinition:
    @schedule(
        job=job_def,
        cron_schedule=cron,
        execution_timezone="Asia/Shanghai",
        name=name,
        default_status=DefaultScheduleStatus.RUNNING if running else DefaultScheduleStatus.STOPPED,
    )
    def _sched(context: ScheduleEvaluationContext) -> RunRequest:
        return RunRequest(run_key=f"{name}-{context.scheduled_execution_time:%Y%m%d%H%M}")

    return _sched


stage2_verify_schedule = _cron(stage2_verify_job, "30 21 * * *", "stage2_verify_schedule")
leak_replay_schedule = _cron(leak_replay_job, "30 3 * * *", "leak_replay_schedule")
quality_alerts_schedule = _cron(quality_alerts_job, "0 22 * * *", "quality_alerts_schedule")
announcement_reconcile_schedule = _cron(
    announcement_reconcile_job, "0 23 * * 0", "announcement_reconcile_schedule"
)
wind_facts_schedule = _cron(wind_facts_job, "0 21 * * 1", "wind_facts_schedule", running=False)


# --- 失败告警 --------------------------------------------------------------------------


@run_failure_sensor(default_status=DefaultSensorStatus.RUNNING, minimum_interval_seconds=60)
def alert_on_run_failure(context: RunFailureSensorContext) -> None:
    """任何作业运行失败都推送告警（同一作业 6 小时内只推一次，见 `ops/alerts.py` 冷却）。"""
    import os

    run = context.dagster_run
    error = context.failure_event.message or "（无错误信息）"
    with psycopg.connect(os.environ["RAGDEMO_DSN"]) as conn:
        raise_alert(
            conn,
            Alert(
                kind="run_failed",
                severity="critical",
                key=f"run_failed:{run.job_name}",
                title=f"Dagster 作业失败：{run.job_name}",
                detail=f"run_id={run.run_id}\n{error[:500]}",
            ),
        )


ALL_JOBS: "list[JobDefinition | UnresolvedAssetJobDefinition]" = [
    prices_job,
    announcements_job,
    stage2_verify_job,
    leak_replay_job,
    quality_alerts_job,
    announcement_reconcile_job,
    wind_facts_job,
    alert_selftest_job,
]
ALL_SCHEDULES: list[ScheduleDefinition] = [
    prices_schedule,
    announcements_schedule,
    stage2_verify_schedule,
    leak_replay_schedule,
    quality_alerts_schedule,
    announcement_reconcile_schedule,
    wind_facts_schedule,
]
ALL_SENSORS = [alert_on_run_failure]
