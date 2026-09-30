"""字符级闭合：数字闭合看不见单位与中文数字（亿↔万、增长↔下降、三倍……）。

内容字符 = str.isalnum() 为真的字符（汉字、字母、数字）；空白、markdown 语法、标点一律忽略。
零容忍：只要 structured 里某字符出现次数多于 source，就报出来。虚警可接受，漏报不可接受。"""

from __future__ import annotations

from collections import Counter


def _content_chars(text: str) -> Counter[str]:
    return Counter(ch for ch in text if ch.isalnum())


def char_violations(structured: str, source: str) -> list[str]:
    """structured 里出现次数多于 source 的内容字符（去重、排序）。"""
    src = _content_chars(source)
    out = _content_chars(structured)
    return sorted(ch for ch, c in out.items() if c > src.get(ch, 0))
