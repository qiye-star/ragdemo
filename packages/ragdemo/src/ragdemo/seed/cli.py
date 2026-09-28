"""`ragdemo db seed-listing-dates` / `ragdemo db seed-build`：从 docs/csv 生成种子。

两步分开：取上市日期要联网（供应商，经出网代理），构建种子是离线、确定性的——
docs/csv 改了只需重跑 `seed-build`，不必再打一遍供应商。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import click
import psycopg

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.config import load_config
from ragdemo.seed.docs_csv import DOCS_CSV_DIR, SEED_DIR, SOURCES_DIR, build_seed, write_seed
from ragdemo.seed.listing_dates import (
    LISTING_DATES_CSV,
    fetch_listing_dates,
    read_curation,
    write_listing_dates,
)
from ragdemo_core.blob import LocalBlobStore

TUSHARE_REST_URL_ENV = "RAGDEMO_TUSHARE_REST_URL"
WIND_STOCK_URL_ENV = "RAGDEMO_WIND_MCP_URL"
WIND_GLOBAL_URL_ENV = "RAGDEMO_WIND_GLOBAL_MCP_URL"


def _dsn() -> str:
    dsn = os.environ.get("RAGDEMO_DSN", "")
    if not dsn:
        raise click.ClickException("环境变量 RAGDEMO_DSN 未设置（参见 .env.example）")
    return dsn


def _require(env: str) -> str:
    value = os.environ.get(env, "").strip()
    if not value:
        raise click.ClickException(f"环境变量 {env} 未设置（参见 .env.example）")
    return value


@click.command("seed-listing-dates")
def seed_listing_dates_command() -> None:
    """从供应商取种子实体的上市日期，写 db/seed/sources/listing_dates.csv。"""
    rows = read_curation(SOURCES_DIR / "entity_curation.csv")
    rest = HttpClient(
        "tushare-rest",
        _require(TUSHARE_REST_URL_ENV),
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=60),
    )
    _require(WIND_GLOBAL_URL_ENV)
    wind_global = gateway_client_from_env(url_env=WIND_GLOBAL_URL_ENV, provider="wind-mcp")
    wind_stock = (
        gateway_client_from_env(url_env=WIND_STOCK_URL_ENV, provider="wind-mcp")
        if os.environ.get(WIND_STOCK_URL_ENV, "").strip()
        else None
    )
    with psycopg.connect(_dsn()) as conn:
        result = fetch_listing_dates(
            rows,
            rest=rest,
            wind_global=wind_global,
            wind_stock=wind_stock,
            conn=conn,
            blob=LocalBlobStore(load_config().blob_root),
            ingest_run_id="cli:seed-listing-dates",
            today=date.today(),
        )
    write_listing_dates(result.dates, LISTING_DATES_CSV)
    click.echo(f"取到 {len(result.dates)} 家的上市日期，写入 {LISTING_DATES_CSV}")
    for note in result.notes:
        click.echo(f"[降级] {note}", err=True)
    for entity_id, reason in result.failures.items():
        click.echo(f"[失败] {entity_id}: {reason}", err=True)
    if result.failures:
        raise click.ClickException(f"{len(result.failures)} 家没取到上市日期，见上方原因")


@click.command("seed-build")
@click.option(
    "--docs-dir", type=click.Path(path_type=Path), default=DOCS_CSV_DIR, show_default=True
)
@click.option("--seed-dir", type=click.Path(path_type=Path), default=SEED_DIR, show_default=True)
def seed_build_command(docs_dir: Path, seed_dir: Path) -> None:
    """由 docs/csv + db/seed/sources/ 生成 db/seed/*.csv（离线、确定性）。"""
    built = build_seed(docs_dir, seed_dir / "sources", seed_dir)
    write_seed(built, seed_dir)
    for name, rows in built.files.items():
        click.echo(f"{name}: {len(rows)}")
    for warning in built.warnings:
        click.echo(f"[注意] {warning}", err=True)
