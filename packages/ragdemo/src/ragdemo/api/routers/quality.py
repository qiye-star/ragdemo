"""GET /api/quality/* —— 质量门禁看板。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from ragdemo.api.deps import AsOf, ConnDep
from ragdemo.api.queries.quality import METRIC_REGISTRY, get_dashboard, get_metric_history
from ragdemo.api.schemas import DashboardResponse, MetricHistoryResponse, MetricSpec

router = APIRouter(tags=["quality"])


@router.get("/quality/registry", response_model=list[MetricSpec])
def get_metric_registry(as_of: AsOf) -> list[MetricSpec]:
    """指标注册表本身不随 as_of 变化，但仍要求这个参数——约束 4 不留例外。"""
    return list(METRIC_REGISTRY)


@router.get("/quality/dashboard", response_model=DashboardResponse)
def get_quality_dashboard(
    as_of: AsOf,
    conn: ConnDep,
    partition_date: Annotated[str | None, Query()] = None,
) -> DashboardResponse:
    return get_dashboard(conn, as_of=as_of, partition_date=partition_date)


@router.get("/quality/metrics/{metric}/history", response_model=MetricHistoryResponse)
def get_quality_metric_history(
    as_of: AsOf,
    conn: ConnDep,
    metric: Annotated[str, Path()],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    source_id: Annotated[str | None, Query()] = None,
) -> MetricHistoryResponse:
    return get_metric_history(conn, as_of=as_of, metric=metric, days=days, source_id=source_id)
