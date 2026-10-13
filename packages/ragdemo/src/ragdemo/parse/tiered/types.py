"""逐页路由记录。不依赖 parse 包内任何模块（textin.py 会导入它，避免循环）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

Tier = Literal["L0", "L1", "L2", "L3"]
TIERS: tuple[Tier, ...] = ("L0", "L1", "L2", "L3")


@dataclass(frozen=True)
class PageRoute:
    page: int  # 原文档页码，从 1 起
    tier: Tier  # 最终采用的层级
    engine: str  # local_llm:<模型+prompt> / <引擎>:<版本> / <主>:<版本>+<副>:<版本> / textin
    text_layer: bool  # 这一页是否有可用文字层
    reasons: tuple[str, ...] = ()  # 逐级降级原因，按尝试顺序
    elapsed_ms: int = 0  # 路由耗时（不含 L3 的 TextIn 调用）
    local_engine_calls: int = 0  # 自托管引擎调用次数（费用按页摊销用）
    llm_tokens: tuple[int, int] = (0, 0)  # L0 大模型 (输入, 输出)，含失败的尝试
    vision_tokens: tuple[int, int] = (0, 0)  # L2 视觉接口 (输入, 输出)
    textin_billed: bool = False  # 这一页这次是否真的向 TextIn 计费（缓存命中为 False）

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"页码必须从 1 起，收到 {self.page}")
        if self.tier not in TIERS:
            raise ValueError(f"未知层级 {self.tier!r}")
        if self.elapsed_ms < 0:
            raise ValueError(f"耗时不能为负：{self.elapsed_ms}")


def route_to_dict(r: PageRoute) -> dict[str, object]:
    return {
        "page": r.page,
        "tier": r.tier,
        "engine": r.engine,
        "text_layer": r.text_layer,
        "reasons": list(r.reasons),
        "elapsed_ms": r.elapsed_ms,
        "local_engine_calls": r.local_engine_calls,
        "llm_tokens": list(r.llm_tokens),
        "vision_tokens": list(r.vision_tokens),
        "textin_billed": r.textin_billed,
    }


def route_from_dict(d: Mapping[str, Any]) -> PageRoute:
    llm = d.get("llm_tokens") or [0, 0]
    vision = d.get("vision_tokens") or [0, 0]
    return PageRoute(
        page=int(d["page"]),
        tier=cast(Tier, str(d["tier"])),
        engine=str(d["engine"]),
        text_layer=bool(d["text_layer"]),
        reasons=tuple(str(x) for x in d.get("reasons") or []),
        elapsed_ms=int(d.get("elapsed_ms") or 0),
        local_engine_calls=int(d.get("local_engine_calls") or 0),
        llm_tokens=(int(llm[0]), int(llm[1])),
        vision_tokens=(int(vision[0]), int(vision[1])),
        textin_billed=bool(d.get("textin_billed")),
    )


SkipRuleId = Literal["blank_page", "signature_tail", "research_disclaimer_tail"]
SKIP_RULE_IDS: Final[tuple[SkipRuleId, ...]] = (
    "blank_page",
    "signature_tail",
    "research_disclaimer_tail",
)


def parse_skip_rules(raw: str) -> frozenset[SkipRuleId]:
    """`PARSE_SKIP_RULES` 的逗号列表 → 规则集合；未知名一律报错（拼错不能变成"静默不跳"）。"""
    names = {s.strip() for s in raw.split(",") if s.strip()}
    unknown = sorted(names - set(SKIP_RULE_IDS))
    if unknown:
        raise ValueError(f"未知的减页规则：{unknown}（可选 {list(SKIP_RULE_IDS)}）")
    return frozenset(cast(SkipRuleId, n) for n in names)


@dataclass(frozen=True)
class TextInCall:
    """一次 TextIn 调用（整份或一个连续段）。每次计费调用对应一条 provider_snapshot。"""

    pages: tuple[int, ...]  # 原文档页码，升序、连续
    json_ref: str | None
    md_ref: str | None
    engine: str  # TextIn engine_version：textin:<版本>+<参数指纹>
    billed_pages: int  # 本次实际计费页数；缓存命中为 0

    def __post_init__(self) -> None:
        if not self.pages or list(self.pages) != sorted(set(self.pages)) or self.pages[0] < 1:
            raise ValueError(f"TextInCall.pages 必须是升序、无重复、从 1 起的页码：{self.pages}")
        if self.billed_pages < 0:
            raise ValueError(f"计费页数不能为负：{self.billed_pages}")


def call_to_dict(c: TextInCall) -> dict[str, object]:
    return {"pages": list(c.pages), "json_ref": c.json_ref, "md_ref": c.md_ref, "engine": c.engine,
            "billed_pages": c.billed_pages}


def call_from_dict(d: Mapping[str, Any]) -> TextInCall:
    return TextInCall(
        pages=tuple(int(p) for p in d["pages"]),
        json_ref=None if d.get("json_ref") is None else str(d["json_ref"]),
        md_ref=None if d.get("md_ref") is None else str(d["md_ref"]),
        engine=str(d["engine"]),
        billed_pages=int(d["billed_pages"]),
    )


@dataclass(frozen=True)
class TextInCall:
    """一次 TextIn 调用（整份或一个连续段）。每次计费调用对应一条 provider_snapshot。"""

    pages: tuple[int, ...]  # 原文档页码，升序、连续
    json_ref: str | None
    md_ref: str | None
    engine: str  # TextIn engine_version：textin:<版本>+<参数指纹>
    billed_pages: int  # 本次实际计费页数；缓存命中为 0

    def __post_init__(self) -> None:
        if not self.pages or list(self.pages) != sorted(set(self.pages)) or self.pages[0] < 1:
            raise ValueError(f"TextInCall.pages 必须是升序、无重复、从 1 起的页码：{self.pages}")
        if self.billed_pages < 0:
            raise ValueError(f"计费页数不能为负：{self.billed_pages}")


def call_to_dict(c: TextInCall) -> dict[str, object]:
    return {"pages": list(c.pages), "json_ref": c.json_ref, "md_ref": c.md_ref, "engine": c.engine,
            "billed_pages": c.billed_pages}


def call_from_dict(d: Mapping[str, Any]) -> TextInCall:
    return TextInCall(
        pages=tuple(int(p) for p in d["pages"]),
        json_ref=None if d.get("json_ref") is None else str(d["json_ref"]),
        md_ref=None if d.get("md_ref") is None else str(d["md_ref"]),
        engine=str(d["engine"]),
        billed_pages=int(d["billed_pages"]),
    )
