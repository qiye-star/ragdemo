"""api/settings.py：两种连接模式的构造与失败路径（无需数据库）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragdemo.api.settings import ApiSettings, SettingsError, from_env


def test_from_env_requires_api_dsn_or_explicit_privileged_optin() -> None:
    with pytest.raises(SettingsError, match="RAGDEMO_API_DSN"):
        from_env({})


def test_from_env_uses_api_dsn_directly_when_set() -> None:
    settings = from_env({"RAGDEMO_API_DSN": "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo"})
    assert settings.dsn == "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo"
    assert settings.set_role is None


def test_from_env_privileged_fallback_sets_role_app_diag() -> None:
    settings = from_env(
        {
            "RAGDEMO_API_ALLOW_PRIVILEGED_DSN": "1",
            "RAGDEMO_DSN": "postgresql://postgres:ragdemo@127.0.0.1:5433/ragdemo",
        }
    )
    assert settings.dsn == "postgresql://postgres:ragdemo@127.0.0.1:5433/ragdemo"
    assert settings.set_role == "app_diag"


def test_from_env_privileged_fallback_without_ragdemo_dsn_raises() -> None:
    with pytest.raises(SettingsError, match="RAGDEMO_DSN"):
        from_env({"RAGDEMO_API_ALLOW_PRIVILEGED_DSN": "1"})


def test_from_env_api_dsn_takes_priority_over_privileged_fallback() -> None:
    """两个都设置时，专用登录用户优先——它是更强的保证，不该被开发逃生口盖过。"""
    settings = from_env(
        {
            "RAGDEMO_API_DSN": "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo",
            "RAGDEMO_API_ALLOW_PRIVILEGED_DSN": "1",
            "RAGDEMO_DSN": "postgresql://postgres:ragdemo@127.0.0.1:5433/ragdemo",
        }
    )
    assert settings.set_role is None
    assert settings.dsn == "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo"


def test_from_env_default_web_root_is_web_directory() -> None:
    settings = from_env({"RAGDEMO_API_DSN": "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo"})
    assert settings.web_root == Path("web")


def test_retrieval_models_defaults_to_mock() -> None:
    settings = from_env({"RAGDEMO_API_DSN": "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo"})
    assert settings.retrieval_models == "mock"


def test_retrieval_models_reads_from_env() -> None:
    settings = from_env(
        {
            "RAGDEMO_API_DSN": "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo",
            "RAGDEMO_API_RETRIEVAL_MODELS": "siliconflow",
        }
    )
    assert settings.retrieval_models == "siliconflow"


def test_retrieval_models_rejects_unknown_value() -> None:
    with pytest.raises(SettingsError, match="RAGDEMO_API_RETRIEVAL_MODELS"):
        from_env(
            {
                "RAGDEMO_API_DSN": "postgresql://ragdemo_api@127.0.0.1:5433/ragdemo",
                "RAGDEMO_API_RETRIEVAL_MODELS": "openai",
            }
        )


def test_repr_never_contains_password() -> None:
    settings = ApiSettings(
        dsn="postgresql://ragdemo_api:super-secret-password@127.0.0.1:5433/ragdemo",
        set_role=None,
        web_root=None,
    )
    assert "super-secret-password" not in repr(settings)
