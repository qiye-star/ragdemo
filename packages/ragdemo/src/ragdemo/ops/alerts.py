"""告警出口（阶段 4.3）：失败、超预算、对账不平都从这里出去。

三个渠道，逐个尝试、互不影响（一个渠道故障不该让告警整个丢掉）：
- `log`：结构化 JSON 日志（永远启用，含 `alert_key`）；
- `db`：`audit.alert_log` 落一行（永远启用，记录各渠道送达结果，也是冷却判定的依据）；
- `webhook`：企业微信 / 飞书 / 钉钉 / 通用 JSON，地址由 `ALERT_WEBHOOK_URL` 提供，没配则跳过。

**冷却**：同一 `alert_key` 在 `cooldown` 内只向外推送一次（默认 6 小时），期间的重复告警照常落库
（标 `suppressed`），但不再打扰人。`critical` 也遵守冷却——想让人重新被通知，换 key 或等冷却过去。

**不吞错**：`raise_alert` 自己绝不抛异常给调用方（告警路径不能把正在处理的故障变成另一个故障），
渠道失败写进 `delivered` 并记日志。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import psycopg

Severity = Literal["info", "warning", "critical"]
WEBHOOK_URL_ENV = "ALERT_WEBHOOK_URL"
WEBHOOK_KIND_ENV = "ALERT_WEBHOOK_KIND"
DEFAULT_COOLDOWN = timedelta(hours=6)

logger = logging.getLogger("ragdemo.alerts")

_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: Severity
    key: str
    title: str
    detail: str = ""

    def text(self) -> str:
        body = f"\n{self.detail}" if self.detail else ""
        return f"{_ICON[self.severity]} [ragdemo/{self.kind}] {self.title}{body}"


def webhook_payload(kind: str, alert: Alert) -> dict[str, Any]:
    """各家机器人的最小文本消息格式。`generic` 给自建接收端。"""
    text = alert.text()
    if kind in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if kind == "generic":
        return {
            "kind": alert.kind,
            "severity": alert.severity,
            "key": alert.key,
            "title": alert.title,
            "detail": alert.detail,
        }
    raise ValueError(f"未知的告警渠道类型 {kind!r}（wecom / feishu / dingtalk / generic）")


def _send_webhook(url: str, kind: str, alert: Alert, transport: httpx.BaseTransport | None) -> str:
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            resp = client.post(url, json=webhook_payload(kind, alert))
        if resp.status_code >= 400:
            return f"error: HTTP {resp.status_code}"
        # 企业微信 / 钉钉 / 飞书在业务失败时仍返回 200，错误码在正文里。
        try:
            body = resp.json()
        except ValueError:
            return "ok"
        code = body.get("errcode", body.get("code", body.get("StatusCode", 0)))
        return (
            "ok"
            if code in (0, None)
            else f"error: 渠道返回 {code} {body.get('errmsg', body.get('msg', ''))}"
        )
    except httpx.HTTPError as exc:
        return f"error: {type(exc).__name__}"


def _recently_sent(conn: psycopg.Connection, key: str, cooldown: timedelta, now: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit.alert_log WHERE alert_key = %s AND NOT suppressed"
        " AND raised_at > %s LIMIT 1",
        (key, now - cooldown),
    ).fetchone()
    return row is not None


def raise_alert(
    conn: psycopg.Connection | None,
    alert: Alert,
    *,
    env: Mapping[str, str] | None = None,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    """发出一条告警，返回各渠道的送达结果。永不抛异常。"""
    source = env if env is not None else os.environ
    moment = now or datetime.now(UTC)
    delivered: dict[str, str] = {}
    suppressed = False

    try:
        if conn is not None and _recently_sent(conn, alert.key, cooldown, moment):
            suppressed = True
            delivered["webhook"] = "skipped: 冷却中"
    except Exception as exc:
        logger.warning("alert cooldown lookup failed: %s", exc)

    level = {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.ERROR}[
        alert.severity
    ]
    logger.log(
        level,
        json.dumps(
            {
                "alert_key": alert.key,
                "kind": alert.kind,
                "severity": alert.severity,
                "title": alert.title,
                "detail": alert.detail,
                "suppressed": suppressed,
            },
            ensure_ascii=False,
        ),
    )
    delivered["log"] = "ok"

    if not suppressed:
        url = (source.get(WEBHOOK_URL_ENV) or "").strip()
        if not url:
            delivered["webhook"] = "skipped: 未配置 ALERT_WEBHOOK_URL"
        else:
            try:
                delivered["webhook"] = _send_webhook(
                    url, (source.get(WEBHOOK_KIND_ENV) or "generic").strip(), alert, transport
                )
            except Exception as exc:
                delivered["webhook"] = f"error: {type(exc).__name__}"

    if conn is not None:
        try:
            conn.execute(
                "INSERT INTO audit.alert_log (raised_at, kind, severity, alert_key, title, detail,"
                " delivered, suppressed) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                (
                    moment,
                    alert.kind,
                    alert.severity,
                    alert.key,
                    alert.title,
                    alert.detail,
                    json.dumps(delivered, ensure_ascii=False),
                    suppressed,
                ),
            )
            conn.commit()
            delivered["db"] = "ok"
        except Exception as exc:
            delivered["db"] = f"error: {type(exc).__name__}"
            logger.error("alert_log insert failed: %s", exc)
    return delivered
