"""密钥剥离：provider_snapshot 会原样记录调用参数，token 混在里面就进了数据库。"""
from __future__ import annotations

from ragdemo.adapters.secrets import strip_secrets


def test_known_secret_keys_are_redacted() -> None:
    out = strip_secrets({"token": "abc", "ts_code": "688256.SH"})
    assert out == {"token": "[REDACTED]", "ts_code": "688256.SH"}


def test_matching_is_case_insensitive_and_substring_aware() -> None:
    out = strip_secrets({"API_Key": "k", "access_token": "t", "X-Auth-Secret": "s"})
    assert set(out.values()) == {"[REDACTED]"}


def test_nested_dicts_are_walked() -> None:
    out = strip_secrets({"headers": {"Authorization": "Bearer x"}, "q": "算力"})
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["q"] == "算力"


def test_non_secret_values_are_untouched_and_input_not_mutated() -> None:
    src = {"ts_code": "688256.SH", "limit": 100}
    out = strip_secrets(src)
    assert out == src
    assert out is not src


def test_secret_nested_inside_a_list_is_redacted() -> None:
    src = {"batch": [{"token": "x", "ts_code": "688256.SH"}, {"q": "算力"}]}
    out = strip_secrets(src)
    assert out["batch"][0]["token"] == "[REDACTED]"
    assert out["batch"][0]["ts_code"] == "688256.SH"
    assert out["batch"][1]["q"] == "算力"
    assert out["batch"] is not src["batch"]
    assert out["batch"][0] is not src["batch"][0]
