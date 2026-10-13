"""按环境变量拼出 `AnnouncementSources`（公告双源接入计划 Task 12）。

`ingest/definitions.py`（真实 Dagster 资源）与 `ingest/cli.py`（`docs announcements`
的手动一次性调用）都要构造同一套来源层——单独抽出来，两处各自的判断规则
才不会跑偏。

哪一层的凭据/地址没配，哪一层就是 `None`（cninfo/ifind/edgar）——与
`ingest/definitions.py::price_sources_resource` 同一条规则：本地开发与测试
不该被逼着配齐全部来源才能跑通。披露易（hkexnews）例外：公开网站，不需要
任何凭据，恒不为 `None`。

Wind 曾经是 A 股公告的第四个来源（`WindDocsAdapter`），2026-09-29 实盘验收时
发现这个账号的 Wind 服务范围只覆盖财务/行情/股东/分部数据，不含公告——已移除
（同步改 `ingest/announcements.py`、`sources/routing.py`、ADR-0013）。
"""

from __future__ import annotations

import os

from ragdemo.adapters.cninfo import CninfoAdapter
from ragdemo.adapters.edgar import EdgarAdapter
from ragdemo.adapters.hkexnews import HkexnewsAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.ifind_notice import IfindNoticeAdapter
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.config import RagdemoConfig
from ragdemo.ingest.announcements import AnnouncementSources
from ragdemo.ingest.cninfo_docs import CninfoLayerDeps
from ragdemo.ingest.edgar_index import EdgarLayerDeps
from ragdemo.ingest.hkexnews_docs import HkexnewsLayerDeps
from ragdemo.parse.describer_factory import build_describer
from ragdemo.parse.textin import DocumentParser, MockDocumentParser, TextInParser
from ragdemo.parse.tiered.factory import with_tiered_routing
from ragdemo_core.blob import LocalBlobStore

CNINFO_USER_AGENT_ENV = "RAGDEMO_CNINFO_USER_AGENT"
EDGAR_USER_AGENT_ENV = "EDGAR_USER_AGENT"
# `.env.example` 早就留了这个名字，注释写的就是"公告片段检索"——与
# `Capability.ANNOUNCEMENTS_CN` 的 ifind-notice 层（service="ifind-news"）
# 说的是同一件事，只是在这次接线之前从没有代码真正读过它。
IFIND_URL_ENV = "RAGDEMO_IFIND_MCP_BASE"

_RATE_PER_MINUTE = 60  # 与既有各 ingest-* CLI 命令的保守估计一致，不是任何供应商承诺的 SLA。


def _configured(env: str) -> bool:
    return bool(os.environ.get(env, "").strip())


def _parser_for(cfg: RagdemoConfig) -> DocumentParser:
    if cfg.textin_base_url is None:
        return MockDocumentParser()
    blob = LocalBlobStore(cfg.blob_root)
    return with_tiered_routing(
        TextInParser(
            cfg.require_textin_base_url(),
            blob,
            allow_private=cfg.textin_allow_private,
        ),
        blob,
        cfg,
    )


def _http_client(provider: str, base_url: str, *, user_agent: str | None = None) -> HttpClient:
    return HttpClient(
        provider,
        base_url,
        policy=RetryPolicy(max_attempts=3, backoff_base_s=1.0),
        bucket=TokenBucket(rate_per_minute=_RATE_PER_MINUTE),
        default_headers={"User-Agent": user_agent} if user_agent else {},
    )


def build_announcement_sources(cfg: RagdemoConfig) -> AnnouncementSources:
    blob = LocalBlobStore(cfg.blob_root)
    parser = _parser_for(cfg)
    describer = build_describer(cfg)

    cninfo = None
    cninfo_ua = os.environ.get(CNINFO_USER_AGENT_ENV, "").strip()
    if cninfo_ua:
        cninfo = CninfoLayerDeps(
            adapter=CninfoAdapter(
                _http_client("cninfo", "https://www.cninfo.com.cn", user_agent=cninfo_ua)
            ),
            static_client=_http_client(
                "cninfo-static", "https://static.cninfo.com.cn", user_agent=cninfo_ua
            ),
            parser=parser,
            blob=blob,
            describer=describer,
        )

    ifind = (
        IfindNoticeAdapter(
            gateway_client_from_env(url_env=IFIND_URL_ENV, provider="ifind-notice", sessions=True)
        )
        if _configured(IFIND_URL_ENV)
        else None
    )

    hkexnews_client = _http_client("hkexnews", "https://www1.hkexnews.hk")
    hkexnews = HkexnewsLayerDeps(
        adapter=HkexnewsAdapter(hkexnews_client),
        static_client=hkexnews_client,
        parser=parser,
        blob=blob,
        describer=describer,
    )

    edgar = None
    edgar_ua = os.environ.get(EDGAR_USER_AGENT_ENV, "").strip()
    if edgar_ua:
        edgar = EdgarLayerDeps(
            adapter=EdgarAdapter(
                client=_http_client("edgar", "https://data.sec.gov", user_agent=edgar_ua)
            ),
            archives_client=_http_client(
                "edgar-archives", "https://www.sec.gov", user_agent=edgar_ua
            ),
            parser=parser,
            blob=blob,
            describer=describer,
        )

    return AnnouncementSources(cninfo=cninfo, ifind=ifind, hkexnews=hkexnews, edgar=edgar)
