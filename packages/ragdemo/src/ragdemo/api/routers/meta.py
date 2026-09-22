"""GET /api/meta —— 连接身份体检 + 当前 as_of 下的可见行数。

这条路由本身就是隔离机制第一层佐证：`current_user_is_superuser` 直接
说明这个连接是不是被 app_diag 收窄过——如果它是 true，后面所有"只查
asof 视图"的保证就只剩 RLS 这一层，值得在诊断界面第一眼就看见。
"""

from __future__ import annotations

from fastapi import APIRouter

from ragdemo.api.constants import BANNER
from ragdemo.api.deps import AsOf, ConnDep
from ragdemo.api.schemas import DbIdentity, MetaResponse, VisibleCounts
from ragdemo.api.serialize import as_bool, as_int, as_str
from ragdemo_core.db.session import as_of_session

router = APIRouter(tags=["meta"])


@router.get("/meta", response_model=MetaResponse)
def get_meta(as_of: AsOf, conn: ConnDep) -> MetaResponse:
    with as_of_session(conn, as_of):
        row = conn.execute(
            "SELECT current_user, session_user,"
            " coalesce((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false),"
            " current_setting('server_version'),"
            " (SELECT count(*) FROM asof.document),"
            " (SELECT count(*) FROM asof.doc_block)"
        ).fetchone()
    if row is None:
        raise RuntimeError("SELECT current_user, ... 没有返回任何行，这不应该发生")

    return MetaResponse(
        banner=BANNER,
        read_only=True,
        as_of=as_of.isoformat(),
        db=DbIdentity(
            current_user=as_str(row[0]),
            session_user=as_str(row[1]),
            current_user_is_superuser=as_bool(row[2]),
            server_version=as_str(row[3]),
        ),
        visible=VisibleCounts(documents=as_int(row[4]), blocks=as_int(row[5])),
    )
