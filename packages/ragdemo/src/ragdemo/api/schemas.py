"""诊断接口的响应模型。

用 pydantic `response_model` 有一个额外好处，不只是文档：FastAPI 按模型
字段序列化响应体，模型里没有的字段会被**丢弃**。约束 6/7（私有内容不可见、
不生成论断文案）因此多了一层机械保证——将来谁在某条查询里手滑多选了一列
（比如 content），只要没把它加进对应的响应模型，它就到不了 HTTP 响应。
"""

from __future__ import annotations

from pydantic import BaseModel


class DbIdentity(BaseModel):
    current_user: str
    session_user: str
    current_user_is_superuser: bool
    server_version: str


class VisibleCounts(BaseModel):
    documents: int
    blocks: int


class MetaResponse(BaseModel):
    banner: str
    read_only: bool
    as_of: str
    db: DbIdentity
    visible: VisibleCounts
