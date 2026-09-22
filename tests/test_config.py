"""ragdemo.config：环境配置的读取与校验。

缺失必填值要像 cli.py::_dsn() 一样大声失败——点名哪个变量、指向 .env.example。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ragdemo.config import ConfigError, load_config


def test_defaults_when_env_is_empty() -> None:
    cfg = load_config({})
    assert cfg.blob_root == Path("data/blob")
    assert cfg.textin_base_url is None
    assert cfg.textin_allow_private is False
    assert cfg.textin_max_pages_per_run == 500


def test_blob_root_reads_from_env() -> None:
    cfg = load_config({"RAGDEMO_BLOB_ROOT": "/srv/ragdemo/blob"})
    assert cfg.blob_root == Path("/srv/ragdemo/blob")


def test_textin_base_url_reads_from_env() -> None:
    cfg = load_config({"TEXTIN_BASE_URL": "http://127.0.0.1:8081"})
    assert cfg.textin_base_url == "http://127.0.0.1:8081"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_textin_allow_private_truthy_values(value: str) -> None:
    cfg = load_config({"TEXTIN_ALLOW_PRIVATE": value})
    assert cfg.textin_allow_private is True


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_textin_allow_private_falsy_values(value: str) -> None:
    cfg = load_config({"TEXTIN_ALLOW_PRIVATE": value})
    assert cfg.textin_allow_private is False


def test_textin_max_pages_per_run_reads_from_env() -> None:
    cfg = load_config({"TEXTIN_MAX_PAGES_PER_RUN": "60"})
    assert cfg.textin_max_pages_per_run == 60


def test_textin_max_pages_per_run_rejects_non_integer() -> None:
    with pytest.raises(ConfigError, match="TEXTIN_MAX_PAGES_PER_RUN"):
        load_config({"TEXTIN_MAX_PAGES_PER_RUN": "not-a-number"})


def test_textin_max_pages_per_run_rejects_non_positive() -> None:
    with pytest.raises(ConfigError, match="TEXTIN_MAX_PAGES_PER_RUN"):
        load_config({"TEXTIN_MAX_PAGES_PER_RUN": "0"})


def test_require_textin_base_url_raises_when_missing() -> None:
    cfg = load_config({})
    with pytest.raises(ConfigError, match="TEXTIN_BASE_URL"):
        cfg.require_textin_base_url()


def test_require_textin_base_url_returns_value_when_set() -> None:
    cfg = load_config({"TEXTIN_BASE_URL": "http://127.0.0.1:8081"})
    assert cfg.require_textin_base_url() == "http://127.0.0.1:8081"


def test_textin_cost_per_page_cny_defaults_to_none() -> None:
    cfg = load_config({})
    assert cfg.textin_cost_per_page_cny is None


def test_textin_cost_per_page_cny_reads_from_env() -> None:
    cfg = load_config({"TEXTIN_COST_PER_PAGE_CNY": "0.35"})
    assert cfg.textin_cost_per_page_cny == Decimal("0.35")


def test_textin_cost_per_page_cny_rejects_non_numeric() -> None:
    with pytest.raises(ConfigError, match="TEXTIN_COST_PER_PAGE_CNY"):
        load_config({"TEXTIN_COST_PER_PAGE_CNY": "not-a-number"})


def test_require_textin_cost_per_page_cny_raises_when_missing() -> None:
    cfg = load_config({})
    with pytest.raises(ConfigError, match="TEXTIN_COST_PER_PAGE_CNY"):
        cfg.require_textin_cost_per_page_cny()


def test_require_textin_cost_per_page_cny_returns_value_when_set() -> None:
    cfg = load_config({"TEXTIN_COST_PER_PAGE_CNY": "0.35"})
    assert cfg.require_textin_cost_per_page_cny() == Decimal("0.35")


# --- SiliconFlow（嵌入/重排，adr/0004）---------------------------------
# 同 TEXTIN_BASE_URL 的两段式校验：base_url 没有默认值（写死一个公网地址
# 会让每一份 clone 默认把数据发到境外），但 load_config() 本身不为它缺失
# 报错——只有真正要构造 SiliconFlowEmbedder/Reranker 的调用点才 require。
# 模型名反过来：docs/06-retrieval.md §5 定死了具体模型，所以两个模型名
# *有* 默认值，环境变量只是逃生口。


def test_siliconflow_base_url_defaults_to_none() -> None:
    cfg = load_config({})
    assert cfg.siliconflow_base_url is None


def test_siliconflow_base_url_reads_from_env() -> None:
    cfg = load_config({"SILICONFLOW_BASE_URL": "http://127.0.0.1:8082"})
    assert cfg.siliconflow_base_url == "http://127.0.0.1:8082"


def test_require_siliconflow_base_url_raises_when_missing() -> None:
    cfg = load_config({})
    with pytest.raises(ConfigError, match="SILICONFLOW_BASE_URL"):
        cfg.require_siliconflow_base_url()


def test_require_siliconflow_base_url_returns_value_when_set() -> None:
    cfg = load_config({"SILICONFLOW_BASE_URL": "http://127.0.0.1:8082"})
    assert cfg.require_siliconflow_base_url() == "http://127.0.0.1:8082"


def test_siliconflow_model_names_default_to_the_models_fixed_in_docs() -> None:
    cfg = load_config({})
    assert cfg.siliconflow_embed_model == "BAAI/bge-m3"
    assert cfg.siliconflow_rerank_model == "BAAI/bge-reranker-v2-m3"


def test_siliconflow_model_names_are_overridable() -> None:
    cfg = load_config(
        {"SILICONFLOW_EMBED_MODEL": "custom/embed", "SILICONFLOW_RERANK_MODEL": "custom/rerank"}
    )
    assert cfg.siliconflow_embed_model == "custom/embed"
    assert cfg.siliconflow_rerank_model == "custom/rerank"
