# Task 8 报告：逐页验收接到新路由

## 做了什么
- `parse/acceptance.py`：`ACCEPTANCE_VERSION="acc-v2"`、`DocumentMeta.skipped_pages`、`coverage_ratio()`、`_structure_problems`（跳页不算缺页；跳页与入库页重叠、forced_l3 有跳页 → rejected）。`_p4` 未改。
- 新增 `parse/tiered/acceptance_inputs.py`：`l0_page_input`（休眠，只接受不产生）、`l3_page_input`、`image_ocr_numbers`、`output_numbers`、`RoutedPage`、`build_ledger`、`CAN_SHOW_RAW_AT_PARSE=False`。
- 测试：`tests/parse/test_acceptance.py` 追加 2 例；新增 `tests/parse/tiered/test_acceptance_inputs.py`（9 例）。

## 裁决落实
- L0 v2 未启用：只有 `L3`/`skipped` 会被构造；`l0_page_input` 仅供未来。
- TextIn 页 P3 = 审计口径：`unexpanded_text`（合并格只算一次）+ 仅对比较副本 `heal_spacing`；不做 ±1 页放宽（审计的结论里没用，adjacent_page 只是归类）。
- 图片 OCR 数字：不静默放行。W1 无独立状态，简报也不允许改 acceptance 状态集，因此映射为现有 `textin_unverified`（P3 fail），并在 `DocRecord.reasons` 追加 `pN unverifiable_image：…`；逐页 `image_ocr_numbers` 记在台账里。**需要控制者知晓：没有新增 DocState。**
- 扫描页：P3 `unverifiable_scan`，文档 `accepted_with_scan`，reasons 追加 `unverifiable_scan`；绝不 accepted（有测试）。

## 偏离
1. 简报测试用 `blocks_from_l0_page`（`parse/tiered/blocks.py`）——该模块尚不存在（后续任务），改为在测试里直接构造 `NormalizedBlock`（`block_type` 用有效值 `title`/`paragraph`，不是 `heading`）。
2. `build_ledger` 增加了上述 reasons 追加（简报代码里没有）。
3. 简报代码里的类型/格式经 ruff format 调整，无语义变化。

## TDD 证据
- RED：先写测试，`pytest` 收集期报 `ModuleNotFoundError: ragdemo.parse.tiered.acceptance_inputs` / `ImportError coverage_ratio`。
- GREEN：实现后首轮 3 例失败均为我的测试夹具用了非法 block_type（`heading`），改 `title` 后 59 例（test_acceptance + test_acceptance_inputs + test_audit_textin_pages）全过。
- ruff check/format、`mypy --strict`（acceptance.py、acceptance_inputs.py、新测试）干净。

## 复现审计分布（缓存 JSON + PDF，只读库 SELECT，零调用）
临时脚本在 scratchpad（未入库）。库里现有 34 份现行 TextIn 文档（审计后新增 3 份），按审计 TSV 的 31 个 doc_id 过滤后：
- 比较副本不做 heal（审计"新"口径）：14 accepted / 1 accepted_with_scan / 16 textin_unverified；页 142 pass / 32 fail / 8 scan。
- `build_ledger` 实际口径（heal）：20 / 1 / 10；页 155 / 19 / 8。
- 逐文档与 `textin-page-audit-2026-10-13-docs.tsv` 的 `verdict` / `verdict_healed` 比对：0 处不一致。
- 另验：2 份文档有"图片 OCR 且 P3 fail"的页，均不为 accepted 且带 `unverifiable_image`；扫描页所在文档无一为 accepted。

## 风险
- 审计脚本 PageInput 的 `page_kind`、`table_blocks` 与真实路由不同：真实路由 table 页若 `table_blocks=0` 会触发 P5 → rejected（调用方 T9 须传入真实块数）。我的复现用 `legacy_unclassified`，所以未触发 P5。
- 无 JSON 的 L3 页输出为空串，P3 通过但 P4 覆盖低：T9 须告警。
- 页码归属偏移（adjacent_page 18 个）仍使 6 份文档停在 textin_unverified，属已知。
