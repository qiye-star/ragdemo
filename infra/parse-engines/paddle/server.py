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


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


# 子流水线开关（构造参数名以容器内 PPStructureV3.__init__ 的签名为准）。默认关掉我们不用的重模型以省内存：
# 文档方向分类、文档矫正、印章、图表、公式。保留版面检测、表格结构识别、文字检测/识别。
# 环境变量 PARSE_USE_<名字>=1 可单独打开。
SWITCHES = {
    "use_doc_orientation_classify": _flag("PARSE_USE_DOC_ORIENTATION_CLASSIFY", False),
    "use_doc_unwarping": _flag("PARSE_USE_DOC_UNWARPING", False),
    "use_seal_recognition": _flag("PARSE_USE_SEAL_RECOGNITION", False),
    "use_chart_recognition": _flag("PARSE_USE_CHART_RECOGNITION", False),
    "use_formula_recognition": _flag("PARSE_USE_FORMULA_RECOGNITION", False),
    "use_table_recognition": _flag("PARSE_USE_TABLE_RECOGNITION", True),
}
# 可选开关：未设置环境变量时不传（沿用库默认）。设 PARSE_USE_REGION_DETECTION=0 /
# PARSE_USE_TEXTLINE_ORIENTATION=0 可关闭对应子模型。
for _env, _arg in (
    ("PARSE_USE_REGION_DETECTION", "use_region_detection"),
    ("PARSE_USE_TEXTLINE_ORIENTATION", "use_textline_orientation"),
):
    if os.environ.get(_env):
        SWITCHES[_arg] = _flag(_env, True)
PIPELINE = PPStructureV3(device=DEVICE, markdown_ignore_labels=[], **SWITCHES)


def parse(path: Path) -> str:
    results = PIPELINE.predict(input=str(path), layout_threshold=LAYOUT_THRESHOLD)
    parts = [res.markdown["markdown_texts"] for res in results]
    return "\n\n".join(parts)


if __name__ == "__main__":
    serve("paddle", version("paddleocr"), DEVICE, parse)
