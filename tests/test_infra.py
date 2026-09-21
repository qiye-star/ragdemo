"""基础设施配置的不变量。这些错误在生产才暴露，代价太高。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

COMPOSE = Path("infra/docker-compose.yml")
POLICY = Path("infra/egress-proxy/policy.yaml")


def test_paradedb_image_version_is_pinned() -> None:
    """adr/0001 的后果 2：镜像版本必须固定，latest 会让 pg_search 语法漂移。"""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    image = compose["services"]["paradedb"]["image"]
    assert ":" in image
    assert not image.endswith(":latest")


def test_database_port_is_bound_to_localhost_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for mapping in compose["services"]["paradedb"]["ports"]:
        assert str(mapping).startswith("127.0.0.1:")


def test_egress_policy_denies_by_default() -> None:
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    assert policy["default_action"] == "deny"


def test_egress_policy_contains_no_literal_secrets() -> None:
    """密钥只在环境变量或代理层注入，策略文件里只能出现变量名。"""
    text = POLICY.read_text(encoding="utf-8")
    policy = yaml.safe_load(text)
    for rule in policy["allowlist"]:
        credential = rule.get("inject_credential")
        if credential is None:
            continue
        # TextIn 一次要注入两个头（x-ti-app-id / x-ti-secret-code），
        # 所以 inject_credential 允许是列表，见 09-compliance-security.md §4.1。
        names = credential if isinstance(credential, list) else [credential]
        for name in names:
            assert name.isupper(), f"{name} 应是环境变量名而非字面量"


def test_egress_policy_has_no_token_shaped_literals() -> None:
    """策略文件里不得出现像密钥的长串字面量。

    上一个测试只能证明「变量名长得像变量名」，证明不了没人把真实值粘进来。
    真实凭据是 32 位十六进制（TextIn）或更长的随机串，这里直接扫形状。
    """
    text = POLICY.read_text(encoding="utf-8")
    suspects = re.findall(r"\b[0-9a-fA-F]{24,}\b|\b[A-Za-z0-9_\-]{40,}\b", text)
    assert not suspects, f"策略文件里出现疑似密钥字面量：{suspects}"


def test_egress_policy_allows_textin() -> None:
    """adr/0008 的文档解析走 api.textin.com，不在白名单里解析会被代理拒掉。"""
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    hosts = {rule["host"] for rule in policy["allowlist"]}
    assert "api.textin.com" in hosts


def test_compose_has_no_hardcoded_password() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD" in text


def test_backup_script_is_executable_and_has_retention() -> None:
    script = Path("scripts/backup.sh").read_text(encoding="utf-8")
    assert "pg_dump" in script
    assert "RETENTION_DAYS" in script


def test_restore_script_refuses_without_explicit_confirmation() -> None:
    """恢复会覆盖现有库，必须显式确认。"""
    script = Path("scripts/restore.sh").read_text(encoding="utf-8")
    assert "CONFIRM" in script


def test_runbook_covers_the_drill() -> None:
    runbook = Path("infra/runbook-backup.md").read_text(encoding="utf-8")
    for heading in ("每日备份", "恢复演练", "PITR"):
        assert heading in runbook


# --- 以下为 Chroma 引入后追加的不变量（adr/0009：Chroma 承担向量召回候选生成） ---


def test_chroma_image_version_is_pinned() -> None:
    """adr/0009 边界：Chroma 只能本地自建；版本固定的理由与 paradedb 相同——
    漂移会让服务端行为（含与已锁定客户端版本的协议兼容性）静默改变。
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    image = compose["services"]["chroma"]["image"]
    assert ":" in image
    assert not image.endswith(":latest")


def test_chroma_port_is_bound_to_localhost_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for mapping in compose["services"]["chroma"]["ports"]:
        assert str(mapping).startswith("127.0.0.1:")


def test_chroma_uses_named_volume_for_persistence() -> None:
    """数据要经得住容器重建：必须是顶层具名 volume，不是匿名 volume 或裸挂载。"""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service_volumes = compose["services"]["chroma"]["volumes"]
    top_level_volumes = compose.get("volumes", {})
    assert service_volumes, "chroma 服务必须声明持久化 volume"
    for mount in service_volumes:
        name = str(mount).split(":", 1)[0]
        assert name in top_level_volumes, f"{name} 必须是顶层具名 volume，而不是裸路径挂载"


def test_backup_script_covers_both_paradedb_and_chroma() -> None:
    """adr/0009 后果 1：多了一个有状态服务要备份，PG 与 Chroma 都不能漏。"""
    script = Path("scripts/backup.sh").read_text(encoding="utf-8")
    assert "pg_dump" in script
    assert "chroma" in script.lower()


def test_restore_script_covers_both_paradedb_and_chroma() -> None:
    script = Path("scripts/restore.sh").read_text(encoding="utf-8")
    assert "pg_restore" in script
    assert "chroma" in script.lower()


def test_runbook_documents_pg_chroma_snapshot_alignment() -> None:
    """两者快照没有对齐到同一时点，恢复后会出现召回集与事实集不一致（adr/0009 后果 1）。"""
    runbook = Path("infra/runbook-backup.md").read_text(encoding="utf-8")
    assert "Chroma" in runbook
    assert "同一时点" in runbook
