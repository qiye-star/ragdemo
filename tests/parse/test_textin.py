"""xParse 封装。

最要紧的三条：从 detail[] 切块（markdown 没有页码）、
parse_engine 带参数指纹（否则评测基线静默失效）、私有文档不外送。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from ragdemo.parse.textin import (
    XPARSE_PARAMS,
    PageBudget,
    ParseConfigError,
    ParsePermanent,
    ParseRetryable,
    PrivateDocumentEgressBlocked,
    TextInParser,
    artifact_keys,
    blocks_from_detail,
    param_fingerprint,
    table_markdown,
)
from ragdemo_core.blob import LocalBlobStore

FIXTURE = Path("tests/fixtures/xparse/annual_report.json")


def _payload() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FIXTURE.read_text(encoding="utf-8")))


def _detail() -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _payload()["result"]["detail"])


# --- 参数指纹 ---------------------------------------------------------------

def test_fingerprint_is_stable_across_key_order() -> None:
    assert param_fingerprint({"a": 1, "b": 2}) == param_fingerprint({"b": 2, "a": 1})


def test_fingerprint_changes_when_a_parameter_changes() -> None:
    """参数一改切块结果就变，评测集的 gold_block_ids 会静默失效（05 §2.2）。"""
    other = {**XPARSE_PARAMS, "table_flavor": "md"}
    assert param_fingerprint(XPARSE_PARAMS) != param_fingerprint(other)


def test_artifact_keys_carry_both_hash_and_fingerprint() -> None:
    j, m = artifact_keys("abc123", "deadbeef")
    assert j == "parse/textin/abc123/deadbeef.json"
    assert m == "parse/textin/abc123/deadbeef.md"


# --- detail[] → NormalizedBlock ---------------------------------------------

def test_headers_and_footers_are_dropped() -> None:
    """content == 1 是供应商标注的非正文（05 §3.3）。"""
    texts = [b.content for b in blocks_from_detail(_detail(), page_dims={})]
    assert "寒武纪 2024 年半年度报告" not in texts


def test_outline_level_drives_block_type() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    by_text = {b.content: b.block_type for b in blocks}
    assert by_text["第三节 主营业务"] == "title"
    assert by_text["公司主营云端训练芯片与智能计算集群系统。"] == "paragraph"


def test_section_path_is_built_from_the_heading_stack() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    body = next(b for b in blocks if b.content.startswith("公司主营"))
    assert body.section_path == "第三节 主营业务 > 3.2 分部收入"


def test_a_heading_gets_its_parents_path_not_its_own() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    sub = next(b for b in blocks if b.content == "3.2 分部收入")
    assert sub.section_path == "第三节 主营业务"


def test_table_becomes_one_block_of_type_table() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    tables = [b for b in blocks if b.block_type == "table"]
    assert len(tables) == 1
    assert "智能计算" in tables[0].content


def test_ordinals_are_contiguous_from_zero() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={})
    assert [b.ordinal for b in blocks] == list(range(len(blocks)))


def test_page_numbers_are_one_based() -> None:
    """元数据校验第 6 条要求 page ∈ [1, page_count]，差一会让整份文档回滚。"""
    blocks = blocks_from_detail(_detail(), page_dims={})
    assert min(b.page for b in blocks if b.page is not None) == 1


def test_zero_based_page_ids_are_shifted_up() -> None:
    detail = [dict(d, page_id=int(d["page_id"]) - 1) for d in _detail()]
    blocks = blocks_from_detail(detail, page_dims={})
    assert min(b.page for b in blocks if b.page is not None) == 1


def test_bbox_is_normalised_by_page_size() -> None:
    blocks = blocks_from_detail(_detail(), page_dims={1: (600, 850), 2: (600, 850)})
    heading = next(b for b in blocks if b.content == "第三节 主营业务")
    assert heading.bbox == pytest.approx((0.0, 60 / 850, 1.0, 100 / 850))


def test_bbox_is_none_when_page_size_is_unknown() -> None:
    """bbox 可空，P1 没有依赖它的功能——为它让整份文档失败不值得。"""
    blocks = blocks_from_detail(_detail(), page_dims={})
    assert all(b.bbox is None for b in blocks)


# --- 表格 -------------------------------------------------------------------

def test_merged_cells_are_expanded_not_left_blank() -> None:
    """留空会让跨列表头匹配不到——BM25 与嵌入都按块的整体文本工作。"""
    md = table_markdown([
        {"row": 0, "col": 0, "row_span": 1, "col_span": 1, "text": "业务分部"},
        {"row": 0, "col": 1, "row_span": 1, "col_span": 2, "text": "2024H1"},
    ])
    assert md.splitlines()[0] == "| 业务分部 | 2024H1 | 2024H1 |"


def test_pipe_inside_a_cell_is_escaped() -> None:
    md = table_markdown([{"row": 0, "col": 0, "row_span": 1, "col_span": 1, "text": "a|b"}])
    assert md.splitlines()[0] == r"| a\|b |"


def test_empty_cells_produce_empty_string() -> None:
    assert table_markdown([]) == ""


# --- 私有材料闸门 -----------------------------------------------------------

def test_private_document_is_not_sent_upstream(tmp_path: Path) -> None:
    """换到托管 API 后新增的风险，MinerU 时代不存在（adr/0008 后果 1）。"""
    parser = TextInParser("http://proxy.invalid", LocalBlobStore(tmp_path))
    with pytest.raises(PrivateDocumentEgressBlocked):
        parser.parse(b"%PDF-1.4", owner_user="u-42")


def test_private_document_passes_when_the_gate_is_open(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob, allow_private=True)
    key, _ = artifact_keys(parser.content_hash(b"%PDF-1.4"), parser.param_fp)
    blob.put(key, json.dumps(_payload(), ensure_ascii=False).encode("utf-8"))
    assert parser.parse(b"%PDF-1.4", owner_user="u-42").from_cache is True


# --- 缓存 -------------------------------------------------------------------

def test_cache_hit_skips_the_billed_api_call(tmp_path: Path) -> None:
    """xParse 按页计费。同一份文档在同一套参数下永远只解析一次（05 §2.4）。"""
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob)
    json_key, _ = artifact_keys(parser.content_hash(b"%PDF-1.4"), parser.param_fp)
    blob.put(json_key, json.dumps(_payload(), ensure_ascii=False).encode("utf-8"))

    result = parser.parse(b"%PDF-1.4")          # base_url 不可达，命中缓存才不会炸

    assert result.from_cache is True
    assert result.json_ref == json_key
    assert result.blocks


def test_engine_version_carries_vendor_version_and_fingerprint(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob)
    json_key, _ = artifact_keys(parser.content_hash(b"x"), parser.param_fp)
    blob.put(json_key, json.dumps(_payload(), ensure_ascii=False).encode("utf-8"))

    assert parser.parse(b"x").engine_version == f"textin:4.2.1+{parser.param_fp}"


# --- 错误码分类 -------------------------------------------------------------

@pytest.mark.parametrize(("code", "marker"), [
    (40303, "unsupported"), (40301, "unsupported"), (40425, "unsupported"),
    (40302, "too_large"), (40422, "corrupt"), (40423, "encrypted"),
])
def test_permanent_failures_carry_a_marker(code: int, marker: str) -> None:
    """永久失败要留记号，否则下次分区重跑会再拉一遍、再失败一遍。"""
    with pytest.raises(ParsePermanent) as excinfo:
        TextInParser.raise_for_code(code)
    assert excinfo.value.marker == marker


@pytest.mark.parametrize("code", [40004, 40101, 40102, 40103, 40424, 40427])
def test_config_errors_fail_loudly(code: int) -> None:
    """这些是我们的 bug，不是数据问题。重试没有意义。"""
    with pytest.raises(ParseConfigError):
        TextInParser.raise_for_code(code)


def test_insufficient_balance_is_not_retryable() -> None:
    """单独列出来：重试会在没钱的时候把分区反复跑满。"""
    with pytest.raises(ParseConfigError, match="余额"):
        TextInParser.raise_for_code(40003)


@pytest.mark.parametrize("code", [30203, 500])
def test_service_faults_are_retryable(code: int) -> None:
    with pytest.raises(ParseRetryable):
        TextInParser.raise_for_code(code)


def test_unknown_code_is_treated_as_retryable() -> None:
    """退避三次后失败，比永久丢掉一份文档安全。"""
    with pytest.raises(ParseRetryable):
        TextInParser.raise_for_code(49999)


def test_partial_page_failure_is_a_warning_not_an_error(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    parser = TextInParser("http://proxy.invalid", blob)
    payload = _payload()
    payload["code"] = 50207
    payload["result"]["success_count"] = 1
    json_key, _ = artifact_keys(parser.content_hash(b"x"), parser.param_fp)
    blob.put(json_key, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    result = parser.parse(b"x")

    assert result.blocks
    assert any("partial" in w for w in result.warnings)


# --- 页数预算 ---------------------------------------------------------------

def test_budget_reports_exhaustion() -> None:
    budget = PageBudget(remaining=10)
    budget.charge(4)
    assert budget.exhausted is False
    budget.charge(6)
    assert budget.exhausted is True
