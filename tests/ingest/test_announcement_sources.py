"""按环境变量拼 `AnnouncementSources`（公告双源接入计划 Task 12）：哪一层的凭据/
地址没配，哪一层就是 `None`——`docs announcements` CLI 与 Dagster 的
`announcement_sources` 资源共用这份构造逻辑，不该被逼着配齐全部来源才能跑通。

Wind 曾经也是一层，2026-09-29 实盘验收时发现这个账号的 Wind 服务范围不含公告，
已移除（见 `ingest/announcement_sources.py` 模块 docstring）。
"""

from __future__ import annotations

import pytest

from ragdemo.config import load_config
from ragdemo.ingest.announcement_sources import (
    CNINFO_USER_AGENT_ENV,
    EDGAR_USER_AGENT_ENV,
    IFIND_URL_ENV,
    build_announcement_sources,
)


def test_unconfigured_layers_are_none_but_hkexnews_always_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env in (CNINFO_USER_AGENT_ENV, EDGAR_USER_AGENT_ENV, IFIND_URL_ENV):
        monkeypatch.delenv(env, raising=False)

    sources = build_announcement_sources(load_config())

    assert sources.cninfo is None
    assert sources.ifind is None
    assert sources.edgar is None
    assert sources.hkexnews is not None  # 披露易是公开网站，不需要任何凭据


def test_cninfo_layer_is_wired_when_user_agent_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CNINFO_USER_AGENT_ENV, "ragdemo research contact@example.invalid")
    sources = build_announcement_sources(load_config())
    assert sources.cninfo is not None


def test_edgar_layer_is_wired_when_user_agent_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EDGAR_USER_AGENT_ENV, "ragdemo research contact@example.invalid")
    sources = build_announcement_sources(load_config())
    assert sources.edgar is not None


def test_ifind_layer_is_wired_when_gateway_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IFIND_URL_ENV, "https://gateway.internal:8443")
    sources = build_announcement_sources(load_config())
    assert sources.ifind is not None
