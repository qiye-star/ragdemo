"""GET /api/isolation/probes、/api/isolation/catalog —— 权限隔离演示。

两条路由都不接受任何身份/表名参数：探针的四个分支来自
`queries/isolation.py::PROBES` 硬编码元组，不来自请求——这是约束 6
（不由请求参数控制）在这个模块里的直接体现。
"""

from __future__ import annotations

from fastapi import APIRouter

from ragdemo.api.deps import AsOf, ConnDep
from ragdemo.api.queries.isolation import get_catalog, run_probes
from ragdemo.api.schemas import CatalogResponse, ProbesResponse

router = APIRouter(tags=["isolation"])


@router.get("/isolation/probes", response_model=ProbesResponse)
def get_isolation_probes(as_of: AsOf, conn: ConnDep) -> ProbesResponse:
    return run_probes(conn, as_of)


@router.get("/isolation/catalog", response_model=CatalogResponse)
def get_isolation_catalog(as_of: AsOf, conn: ConnDep) -> CatalogResponse:
    return get_catalog(conn, as_of=as_of)
