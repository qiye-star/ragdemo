"""数字召回的缺失归因（纯函数）：参考（TextIn）里有、候选（L0）里没有的数字，各自为什么没有。

- figure_stripped：随图表区域一起剥离的文字里（图表数据不提取，用户 2026-10-13 裁决）；
- decor_stripped：在被剥离的装饰文字（页眉页脚 / 页码 / 旋转字）里；
- unexplained：文字层里还有富余（没被输出用掉）的同一个数字，输出却少了——构造上不可能，出现即 bug；
- not_in_text_layer：数字串在文字层里根本不存在（TextIn 从图片里 OCR 的）；
- tokenization：数字串在文字层里，但分词不同（跨词、与相邻字符连写等），
  逐个列入 examples 供人工复核。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ragdemo.parse.local.number_closure import fold_text, numbers_in

_NON_DIGIT = re.compile(r"\D")


def _digits(text: str) -> str:
    return _NON_DIGIT.sub("", fold_text(text))


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
    reference: str, candidate: str, *, layer_text: str, stripped_text: str, figure_text: str = ""
) -> MissingNumbers:
    ref, cand = numbers_in(reference), numbers_in(candidate)
    figure = numbers_in(figure_text)
    stripped = numbers_in(stripped_text)
    spare = numbers_in(layer_text) - cand  # 文字层里没被输出用掉的数字
    layer_digits = _digits(layer_text + "\n" + stripped_text + "\n" + figure_text)
    counts: Counter[str] = Counter()
    examples: list[str] = []
    for n, c in sorted((ref - cand).items()):
        for _ in range(c):
            if figure[n] > 0:
                figure[n] -= 1
                counts["figure"] += 1
            elif stripped[n] > 0:
                stripped[n] -= 1
                counts["decor"] += 1
            elif spare[n] > 0:
                spare[n] -= 1
                counts["unexplained"] += 1
                examples.append(n)
            elif _digits(n) not in layer_digits:
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
