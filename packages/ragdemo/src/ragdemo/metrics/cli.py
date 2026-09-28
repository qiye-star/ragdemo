"""`ragdemo metrics`：口径（指标定义 / 字段映射 / AI 收入拆分规则）的 Excel 导出与导入。

走命令行而不是网页上传：诊断界面按 ADR-0010 只读，不开写接口。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import click
import psycopg

from ragdemo.metrics.definitions import PublishOutcome
from ragdemo.metrics.importer import ImportRejected, apply_import, plan_import
from ragdemo.metrics.workbook import WorkbookError, export_template, read_workbook

_OUTCOME_LABEL = {
    PublishOutcome.CREATED: "新增",
    PublishOutcome.NEW_VERSION: "新版本",
    PublishOutcome.UNCHANGED: "不变",
}


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


@click.group("metrics")
def metrics_group() -> None:
    """口径的 Excel 导出与导入（改口径 = 追加新版本，旧版本永久保留）。"""


@metrics_group.command("export-template")
@click.option("--out", "out", required=True, type=click.Path(dir_okay=False, path_type=Path))
def export_command(out: Path) -> None:
    """导出当前生效的口径到 Excel，作为编辑的起点。"""
    with psycopg.connect(_dsn()) as conn:
        export_template(conn, out)
    click.echo(f"已导出到 {out}（改完用 ragdemo metrics import --file {out} --author <你的名字>）")


@metrics_group.command("import")
@click.option(
    "--file", "path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--author", required=True, help="修改人，写进每个新版本")
@click.option("--note", default=None, help="这次修改的说明，写进每个新版本")
@click.option("--yes", is_flag=True, default=False, help="真正写库；不传则只预览差异")
def import_command(path: Path, author: str, note: str | None, yes: bool) -> None:
    """校验 Excel、预览与当前口径的差异；加 --yes 才按版本追加写入（整批一个事务）。"""
    try:
        data = read_workbook(path)
    except Exception as exc:  # 文件损坏 / 不是 xlsx：给录入者一句能看懂的话
        raise click.ClickException(f"读不了 {path}：{exc}") from exc

    with psycopg.connect(_dsn()) as conn:
        try:
            plan = plan_import(conn, data)
        except WorkbookError as exc:
            raise click.ClickException(str(exc)) from None

        changed = plan.changed()
        for change in changed:
            click.echo(f"[{_OUTCOME_LABEL[change.outcome]}] {change.sheet} {change.key}")
            for name, before, after in change.diffs:
                click.echo(f"    {name}: {before!r} → {after!r}")
        unchanged = len(plan.changes) - len(changed)
        click.echo(f"共 {len(plan.changes)} 行：{len(changed)} 行有变化，{unchanged} 行不变")

        if not yes:
            click.echo("[dry run] 未写库；确认上面的差异无误后加 --yes 写入")
            return
        if not changed:
            click.echo("没有需要写入的变化")
            return
        try:
            counts = apply_import(
                conn, plan, author=author, change_note=note, known_at=datetime.now(UTC)
            )
        except ImportRejected as exc:
            raise click.ClickException(str(exc)) from None
    click.echo(
        f"已写入：新增 {counts[PublishOutcome.CREATED]}，"
        f"新版本 {counts[PublishOutcome.NEW_VERSION]}（旧版本保留）"
    )
