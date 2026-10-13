"""按配置决定是否用 TieredParser 包装 TextIn。关闭时原样返回，线上行为与今天逐字节一致。

L0 v2 未通过 G9，不启用：配置里的 L0 开关为真（只可能是手工构造的配置，load_config 已拒绝环境变量）
在这里再拒绝一次，消息固定为 L0_REFUSED_MESSAGE。回滚只需把 PARSE_TIERED_ENABLED 改回 false。"""

from __future__ import annotations

from ragdemo.config import L0_REFUSED_MESSAGE, ConfigError, RagdemoConfig
from ragdemo.parse.textin import DocumentParser
from ragdemo.parse.tiered.parser import TieredParser
from ragdemo_core.blob import BlobStore


def with_tiered_routing(
    textin: DocumentParser, blob: BlobStore, cfg: RagdemoConfig
) -> DocumentParser:
    if cfg.parse_tier_l0_enabled:
        raise ConfigError(L0_REFUSED_MESSAGE)
    if not cfg.parse_tiered_enabled or not cfg.parse_tiered_doc_types:
        return textin
    return TieredParser(
        textin,
        blob,
        doc_types=cfg.parse_tiered_doc_types,
        enforced_skips=cfg.parse_skip_rules,
        textin_fp=str(getattr(textin, "param_fp", type(textin).__name__)),
    )
