from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ragdemo.config import ConfigError, load_config
from ragdemo.parse.textin import DocTypeAwareParser, MockDocumentParser
from ragdemo.parse.tiered.factory import with_tiered_routing
from ragdemo.parse.tiered.parser import TieredParser
from ragdemo_core.blob import LocalBlobStore


def test_switches_off_return_the_parser_unchanged(tmp_path: Path) -> None:
    textin = MockDocumentParser()
    blob = LocalBlobStore(tmp_path)
    assert with_tiered_routing(textin, blob, load_config({})) is textin
    only_master = load_config({"PARSE_TIERED_ENABLED": "true"})
    assert with_tiered_routing(textin, blob, only_master) is textin


def test_switches_on_wrap_it_in_a_tiered_parser_without_skips(tmp_path: Path) -> None:
    cfg = load_config({"PARSE_TIERED_ENABLED": "true", "PARSE_TIERED_DOC_TYPES": "research_report"})
    p = with_tiered_routing(MockDocumentParser(), LocalBlobStore(tmp_path), cfg)
    assert isinstance(p, TieredParser) and isinstance(p, DocTypeAwareParser)
    assert p.engine.startswith("tiered:v1+")


def test_skip_rules_reach_the_parser_only_when_configured(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    base = {"PARSE_TIERED_ENABLED": "true", "PARSE_TIERED_DOC_TYPES": "announcement"}
    a = with_tiered_routing(MockDocumentParser(), blob, load_config(base))
    b = with_tiered_routing(
        MockDocumentParser(), blob, load_config({**base, "PARSE_SKIP_RULES": "blank_page"})
    )
    assert isinstance(a, TieredParser) and isinstance(b, TieredParser) and a.engine != b.engine


def test_factory_refuses_l0_even_for_a_hand_built_config(tmp_path: Path) -> None:
    cfg = dataclasses.replace(
        load_config({"PARSE_TIERED_ENABLED": "true", "PARSE_TIERED_DOC_TYPES": "announcement"}),
        parse_tier_l0_enabled=True,
    )
    with pytest.raises(ConfigError, match=r"L0 v2 not validated \(G9\)"):
        with_tiered_routing(MockDocumentParser(), LocalBlobStore(tmp_path), cfg)
