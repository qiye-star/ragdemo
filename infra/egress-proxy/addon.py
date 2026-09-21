"""出网代理的 mitmproxy 插件：白名单放行 + 凭据注入。

这一层存在的理由不是「方便」，而是 docs/09-compliance-security.md §4.1 的两条硬约束：

1. **业务容器的环境变量里没有任何供应商密钥**。密钥只存在于本进程，
   由本插件在转发时注入。业务代码连密钥长什么样都不知道，
   所以它既不可能把密钥写进日志，也不可能把它塞进 `provider_snapshot.params`。
2. **不在白名单的域名直接拒绝**。这同时是防数据外泄的机制——
   即使某个依赖被投毒，它也无法把库里的原始文档发到任意地址
   （CLAUDE.md §0：原始文档、用户上传材料、实体财务数据不得出境）。

策略文件见同目录 policy.yaml。新增供应商改那个文件，不改这里。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import yaml
from mitmproxy import http

POLICY_PATH = os.environ.get("EGRESS_POLICY", "/policy/policy.yaml")

logger = logging.getLogger("egress")


class EgressPolicy:
    def __init__(self, path: str = POLICY_PATH) -> None:
        with open(path, encoding="utf-8") as fh:
            policy: dict[str, Any] = yaml.safe_load(fh)

        self.default_action: str = policy.get("default_action", "deny")
        # 按 host 建索引。策略里 host 是精确匹配，不做通配——
        # 通配符白名单（*.example.com）会在供应商启用用户可控子域时变成开放代理。
        self.rules: dict[str, dict[str, Any]] = {
            rule["host"]: rule for rule in policy.get("allowlist", [])
        }

    def rule_for(self, host: str) -> dict[str, Any] | None:
        return self.rules.get(host)


policy = EgressPolicy()


def _credential_names(rule: dict[str, Any]) -> list[str]:
    """把 inject_credential 归一成列表。

    TextIn 一次要注入两个头，所以这个字段允许是列表
    （09-compliance-security.md §4.1）。
    """
    raw = rule.get("inject_credential")
    if raw is None:
        return []
    return list(raw) if isinstance(raw, list) else [raw]


def _inject(flow: http.HTTPFlow, rule: dict[str, Any]) -> None:
    names = _credential_names(rule)
    if not names:
        return

    values = [os.environ.get(name, "") for name in names]
    if not all(values):
        # 缺凭据就让它带着空值过去、由上游返回 401，比在这里伪造一个成功更安全：
        # 静默放行会让「凭据没配」表现为「供应商偶发失败」，排查成本极高。
        missing = [n for n, v in zip(names, values, strict=True) if not v]
        logger.warning("missing credential env vars: %s", ",".join(missing))

    mode = rule.get("inject_as", "header")

    if mode == "bearer":
        flow.request.headers["Authorization"] = f"Bearer {values[0]}"
        return

    if mode == "header":
        headers = rule.get("headers") or []
        if len(headers) != len(values):
            logger.error("policy for %s: headers 与 inject_credential 数量不一致", rule["host"])
            return
        for header, value in zip(headers, values, strict=True):
            flow.request.headers[header] = value
        return

    if mode == "body_json":
        # tushare 把 token 放在 JSON body 里。只补 token 字段，不动其他参数。
        try:
            body: dict[str, Any] = json.loads(flow.request.get_text() or "{}")
        except json.JSONDecodeError:
            logger.error("body_json 注入失败：%s 的请求体不是 JSON", rule["host"])
            return
        body["token"] = values[0]
        flow.request.set_text(json.dumps(body, ensure_ascii=False))
        return

    logger.error("未知的 inject_as: %s", mode)


def request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    rule = policy.rule_for(host)

    if rule is None:
        if policy.default_action == "deny":
            # 拒绝事件要告警：它要么是配置漏了一个供应商，要么是有人在往外发数据。
            logger.warning("egress denied host=%s path=%s", host, flow.request.path)
            flow.response = http.Response.make(
                403,
                b'{"error":"egress blocked: host not in allowlist"}',
                {"Content-Type": "application/json"},
            )
        return

    _inject(flow, rule)


def response(flow: http.HTTPFlow) -> None:
    """log: full —— 记录 host / path / 状态码 / 字节数；不记录请求体。

    请求体里有原始文档内容与注入后的密钥，落日志等于把两条硬约束同时破掉。
    """
    if flow.response is None:
        return
    logger.info(
        "egress host=%s path=%s status=%d bytes=%d",
        flow.request.pretty_host,
        flow.request.path,
        flow.response.status_code,
        len(flow.response.content or b""),
    )
