# W2 报告：TextIn 页数字审计

详见 docs/10.12-plan/验收记录/TextIn页数字审计.md（含口径、分类、抽样、建议）。

- 范围：31 份现行 TextIn 文档（superseded_at IS NULL，每个 content_hash 一行），182 页；缓存 JSON 零成本，库只读。
- 逐页 P3：pass 124 / fail 50 / unverifiable_scan 8。
- 文档结论：accepted 14（全是 1-5 页短公告）、accepted_with_scan 1（doc 575 全扫描）、textin_unverified 16。
- 未溯源数字 804/5060（15.9%）：merge 展开 461、图片 OCR 235、spacing 缺陷 59、邻页（页码归属偏移）23、完全缺失 24（图片/标题/列表编号）、格式差 2。去掉前三类产物缺陷后 282 个、8 份文档。
- 反向丢失 300：页眉页脚 249、表格 30、其他 21、图表 0。
- S5 字面触发；建议见文档 §5：图片 OCR 块打标并强制 `推断`、扫描文档降级为数字不可引用、spacing 确定性规整（需评测）、P3 对 TextIn 喂不展开合并格的文本、引用补页跨度。不重解析。
- 提交：见 git log（scripts/audit_textin_pages.py、parse/textin_audit.py、tests/parse/test_audit_textin_pages.py、审计文档）。
