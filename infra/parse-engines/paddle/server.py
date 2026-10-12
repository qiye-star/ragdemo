"""PaddleOCR PP-StructureV3 薄壳。模型在镜像构建期下载，运行期容器无外网。

API 以 T1 记录的 paddleocr 版本为准（3.x：PPStructureV3().predict(input=...)，结果对象的
.markdown 为 dict，正文在 "markdown_texts"）。若该版本属性名不同，按其官方文档改 parse()，
以 Step 6 的冒烟输出含 91.2 为准。版面忽略标签需置空，让页眉、页脚、页码也输出——
否则文字层校验会把它们判为"丢失数字"。"""

from __future__ import annotations

import os
from importlib.metadata import version
from pathlib import Path

from paddleocr import PPStructureV3
from shim import serve

DEVICE = os.environ.get("PARSE_DEVICE", "cpu")
# 版面检测阈值：默认 0.5 会把无框线的稀疏表格（得分 0.49）漏判成文字块，表格的数字列落到
# 任何块之外而丢失（T5 冒烟实测，见 docs/10.12-plan/验收记录/T5-PaddleOCR适配器.md）。
# 降到 0.2 以提高召回；是否过召回由 verify.py 的数字闭合校验兜底。
LAYOUT_THRESHOLD = float(os.environ.get("PARSE_LAYOUT_THRESHOLD", "0.2"))
PIPELINE = PPStructureV3(device=DEVICE, markdown_ignore_labels=[])


def parse(path: Path) -> str:
    results = PIPELINE.predict(input=str(path), layout_threshold=LAYOUT_THRESHOLD)
    parts = [res.markdown["markdown_texts"] for res in results]
    return "\n\n".join(parts)


if __name__ == "__main__":
    serve("paddle", version("paddleocr"), DEVICE, parse)
