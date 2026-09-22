"""诊断接口的连接配置：部署模式与开发模式共享同一个构造入口。

两种模式（docs/superpowers/plans/2026-09-22-web-diagnostic-ui.md 裁决 1）：
- 部署：`RAGDEMO_API_DSN` 已设置——直连一个已被 GRANT app_diag 的登录用户，
  `set_role` 为 None。
- 开发：`RAGDEMO_API_DSN` 未设置，但显式打开
  `RAGDEMO_API_ALLOW_PRIVILEGED_DSN`——用 `RAGDEMO_DSN`（超级用户）连接，
  但连上后必须 `SET ROLE app_diag`（由 deps.py 的连接工厂执行，这里只记录
  「需要 SET ROLE」这个事实）。这不是部署形态：`SET ROLE` 可以被
  `RESET ROLE` 撤销，专用登录用户不能。

两者都不满足 → 拒绝构造，报错点名两个变量与 `.env.example`，同
`cli.py::_dsn()` 的失败风格一致。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_TRUTHY = frozenset({"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"})

DEFAULT_WEB_ROOT = Path("web")


class SettingsError(RuntimeError):
    """诊断接口必填的环境配置缺失或不合法。"""


@dataclass(frozen=True)
class ApiSettings:
    # repr=False：ApiSettings 被打日志或异常回溯捕获时，DSN 里的口令不会
    # 泄漏到日志——同 config.py 的既有信条「密钥只在环境变量」在这里的延伸，
    # 一旦泄漏到日志就等于泄漏到了别处。
    dsn: str = field(repr=False)
    set_role: str | None
    web_root: Path | None


def from_env(env: Mapping[str, str] | None = None) -> ApiSettings:
    """从环境变量（或显式传入的映射，供测试用）构造诊断接口配置并校验。"""
    source: Mapping[str, str] = env if env is not None else os.environ

    api_dsn = (source.get("RAGDEMO_API_DSN") or "").strip()
    privileged_opt_in = (source.get("RAGDEMO_API_ALLOW_PRIVILEGED_DSN") or "").strip() in _TRUTHY

    dsn: str
    set_role: str | None
    if api_dsn:
        dsn = api_dsn
        set_role = None
    elif privileged_opt_in:
        privileged_dsn = (source.get("RAGDEMO_DSN") or "").strip()
        if not privileged_dsn:
            raise SettingsError(
                "RAGDEMO_API_ALLOW_PRIVILEGED_DSN 已打开，但环境变量 RAGDEMO_DSN"
                " 也未设置（参见 .env.example）"
            )
        dsn = privileged_dsn
        set_role = "app_diag"
    else:
        raise SettingsError(
            "既没有设置环境变量 RAGDEMO_API_DSN，也没有打开"
            " RAGDEMO_API_ALLOW_PRIVILEGED_DSN（参见 .env.example）。"
            "诊断接口需要一个明确指向 app_diag 身份的连接，不会替你猜一个默认值。"
        )

    web_root_raw = (source.get("RAGDEMO_API_WEB_ROOT") or "").strip()
    web_root = Path(web_root_raw) if web_root_raw else DEFAULT_WEB_ROOT

    return ApiSettings(dsn=dsn, set_role=set_role, web_root=web_root)
