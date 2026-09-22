"""GET /api/parse/* —— 分档路由、生效策略、月度预算、重试队列、告警。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Query

from ragdemo.api.deps import AsOf, ConnDep
from ragdemo.api.errors import ApiError, ErrorCode
from ragdemo.api.queries.parse_ops import (
    get_budget,
    get_policies,
    get_retry_queue,
    get_tier_distribution,
    get_warnings,
)
from ragdemo.api.schemas import (
    BudgetResponse,
    PoliciesResponse,
    RetryQueueResponse,
    TierDistributionResponse,
    WarningsResponse,
)

router = APIRouter(tags=["parse"])

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@router.get("/parse/tiers", response_model=TierDistributionResponse)
def get_parse_tiers(as_of: AsOf, conn: ConnDep) -> TierDistributionResponse:
    return get_tier_distribution(conn, as_of=as_of)


@router.get("/parse/policies", response_model=PoliciesResponse)
def get_parse_policies(as_of: AsOf, conn: ConnDep) -> PoliciesResponse:
    return get_policies(conn, as_of=as_of)


@router.get("/parse/budget", response_model=BudgetResponse)
def get_parse_budget(
    as_of: AsOf,
    conn: ConnDep,
    month: Annotated[str | None, Query(description="YYYY-MM，缺省取 as_of 所在月")] = None,
) -> BudgetResponse:
    if month is not None and not _MONTH_RE.match(month):
        raise ApiError(
            422,
            ErrorCode.INVALID_PARAM,
            f"month 必须是 YYYY-MM 形式，收到 {month!r}",
            param="month",
        )
    return get_budget(conn, as_of=as_of, month=month)


@router.get("/parse/retry-queue", response_model=RetryQueueResponse)
def get_parse_retry_queue(
    as_of: AsOf,
    conn: ConnDep,
    source: Annotated[str | None, Query()] = None,
) -> RetryQueueResponse:
    return get_retry_queue(conn, as_of=as_of, source=source)


@router.get("/parse/warnings", response_model=WarningsResponse)
def get_parse_warnings(
    as_of: AsOf,
    conn: ConnDep,
    warning: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> WarningsResponse:
    return get_warnings(conn, as_of=as_of, warning=warning, limit=limit)
