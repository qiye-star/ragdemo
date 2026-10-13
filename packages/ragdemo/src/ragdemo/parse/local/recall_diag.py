"""数字召回的缺失归因（纯函数）：参考（TextIn）里有、候选（L0）里没有的数字，各自为什么没有。

- figure_stripped：随图表区域一起剥离的文字里（图表数据不提取，用户 2026-10-13 裁决）；
- decor_stripped：在被剥离的装饰文字（页眉页脚 / 页码 / 旋转字）里；
- unexplained：文字层（正文）里还有富余（没被输出用掉）的同一个数字，输出却少了。**不是**"构造上
  不可能"：只有输出的某页被丢弃 / 数字被排版吞掉时才为 0，分析对象全是 ok 页时恒为 0 只说明没有这类
  文档，不说明没有这类 bug；先于 decor 判定，免得正文丢了的数字被同一个数字的页眉页脚掩盖；
- decor_stripped：在被剥离的装饰文字（页眉页脚 / 页码 / 旋转字）里；
- not_in_text_layer：数字串在文字层里根本不存在（TextIn 从图片里 OCR 的）；
- tokenization：数字串在**同一页**文字层的**同一个数字串**里（小数点 / 千分位逗号两侧空白已愈合），
  但分词不同（与相邻字符连写、拆成多个词等），逐个列入 examples 供人工复核。
  数字串由多个独立的数字段拼出来的（"7" "7" → "77"）、或跨页拼出来的，不算 tokenization。

参考侧先愈合 TextIn 的 "5 . 83" / "5 , 223 , 600"（T4 评审 I4 裁决）：否则一个数字被记成缺 2 个、
参考总数多 1，召回系统性偏低。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ragdemo.parse.local.compare import number_recall_precision
from ragdemo.parse.local.number_closure import fold_text, numbers_in
from ragdemo.parse.textin_audit import heal_spacing

_NON_DIGIT = re.compile(r"\D")
_DIGIT_RUN = re.compile(r"\d[\d.,]*\d|\d")


def normalize_reference_numbers(text: str) -> str:
    """参考侧（TextIn）归一化：'5 . 83' → '5.83'，'5 , 223 , 600' → '5,223,600'。
    只作用于参考文本，永不作用于候选 / 文字层（候选里的空格分隔数字就是两个数字）。"""
    return heal_spacing(text)


def number_recall_precision_normalised(reference: str, candidate: str) -> tuple[float, float]:
    """G3 / G13 口径：参考侧先归一化的数字召回与精确率。"""
    return number_recall_precision(normalize_reference_numbers(reference), candidate)


def _digit_runs(text: str) -> list[str]:
    """一页文字里的数字串：小数点 / 千分位逗号相连的数字归为一串（愈合两侧空白），
    裸空白隔开的数字是不同的串。只留数字。"""
    return [_NON_DIGIT.sub("", m) for m in _DIGIT_RUN.findall(heal_spacing(fold_text(text)))]


@dataclass(frozen=True)
class MissingNumbers:
    reference_total: int = 0
    hit: int = 0
    decor_stripped: int = 0
    tokenization: int = 0
    not_in_text_layer: int = 0
    unexplained: int = 0
    examples: tuple[str, ...] = ()  # tokenization 与 unexplained 的数字
    figure_stripped: int = 0  # 追加在末尾：位置参数构造不受影响

    @property
    def recall(self) -> float:
        return self.hit / self.reference_total if self.reference_total else 1.0

    def plus(self, other: MissingNumbers) -> MissingNumbers:
        return MissingNumbers(
            self.reference_total + other.reference_total,
            self.hit + other.hit,
            self.decor_stripped + other.decor_stripped,
            self.tokenization + other.tokenization,
            self.not_in_text_layer + other.not_in_text_layer,
            self.unexplained + other.unexplained,
            self.examples + other.examples,
            self.figure_stripped + other.figure_stripped,
        )


def classify_missing_numbers(
    reference: str,
    candidate: str,
    layer_text: str,
    stripped_text: str,
    figure_text: str = "",
    layer_pages: Sequence[str] | None = None,
) -> MissingNumbers:
    """layer_pages：逐页文字层（给了就按页匹配 tokenization；不给则整份 layer_text 当一页）。"""
    ref, cand = numbers_in(normalize_reference_numbers(reference)), numbers_in(candidate)
    figure = numbers_in(figure_text)
    stripped = numbers_in(stripped_text)
    spare = numbers_in(layer_text) - cand  # 文字层里没被输出用掉的数字
    pages = list(layer_pages) if layer_pages is not None else [layer_text]
    run_blob = "\n".join(
        sorted({run for page in (*pages, stripped_text, figure_text) for run in _digit_runs(page)})
    )
    counts: Counter[str] = Counter()
    examples: list[str] = []
    for n, c in sorted((ref - cand).items()):
        for _ in range(c):
            if figure[n] > 0:
                figure[n] -= 1
                counts["figure"] += 1
            elif spare[n] > 0:
                spare[n] -= 1
                counts["unexplained"] += 1
                examples.append(n)
            elif stripped[n] > 0:
                stripped[n] -= 1
                counts["decor"] += 1
            elif _NON_DIGIT.sub("", fold_text(n)) not in run_blob:
                counts["absent"] += 1
            else:
                counts["token"] += 1
                examples.append(n)
    hit = sum(min(c, cand.get(n, 0)) for n, c in ref.items())
    return MissingNumbers(
        sum(ref.values()),
        hit,
        counts["decor"],
        counts["token"],
        counts["absent"],
        counts["unexplained"],
        tuple(sorted(set(examples))),
        counts["figure"],
    )
