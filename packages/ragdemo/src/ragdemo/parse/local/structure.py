"""大模型整理一页：只排版，不产生数值。整理结果必须通过数字闭合校验。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ragdemo.adapters.http import HttpClient
from ragdemo.parse.describe_llm import Usage, load_prompt
from ragdemo.parse.local.char_closure import char_violations
from ragdemo.parse.local.number_closure import closure_violations

CHAT_ENDPOINT = "/chat/completions"
PROMPT_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "parse_structure"


class StructureFailed(RuntimeError):
    """这一页的整理结果不可信（截断、空回复、丢数字、编造数字……），应退回 TextIn。"""


class ClosureFailed(StructureFailed):
    """整理后的文本含有源文里没有的数字（重试后仍然如此）。"""

    def __init__(self, violations: list[str]) -> None:
        super().__init__(f"数字闭合校验失败：{violations}")
        self.violations = violations


@dataclass(frozen=True)
class _Problems:
    bad_numbers: list[str]  # 输出里有、源里没有的数字
    lost_numbers: list[str]  # 源正文里有、输出里没有的数字
    bad_chars: list[str]  # 输出里有、源里没有的字（单位 / 中文数字 / 方向词被改）
    lost_chars: list[str]
    missing_tables: list[int]  # 没有原样保留的已还原表格（从 1 起）

    @property
    def ok(self) -> bool:
        return not (
            self.bad_numbers
            or self.lost_numbers
            or self.bad_chars
            or self.lost_chars
            or self.missing_tables
        )

    def retry_message(self) -> str:
        parts: list[str] = []
        if self.bad_numbers:
            parts.append(f"你的输出里出现了输入中没有的数字：{', '.join(self.bad_numbers)}。")
        if self.lost_numbers:
            parts.append(f"你的输出丢失了输入里的数字：{', '.join(self.lost_numbers)}。")
        if self.bad_chars:
            parts.append(f"你的输出里出现了输入中没有的字：{''.join(self.bad_chars[:20])}。")
        if self.lost_chars:
            parts.append(f"你的输出丢失了输入中的字：{''.join(self.lost_chars[:20])}。")
        for n in self.missing_tables:
            parts.append(f"你没有原样保留第 {n} 张已还原的表格。")
        parts.append(
            "请只做排版，不要新增、删改任何文字或数字，表格原样粘贴，重新输出整理后的 markdown。"
        )
        return "".join(parts)

    def failure_reason(self) -> str:
        reasons: list[str] = []
        if self.lost_numbers:
            reasons.append(f"整理后丢失了数字：{self.lost_numbers}")
        if self.bad_chars:
            reasons.append(f"字符闭合失败：新增{''.join(self.bad_chars[:20])}")
        if self.lost_chars:
            reasons.append(f"字符闭合失败：丢失{''.join(self.lost_chars[:20])}")
        if self.missing_tables:
            reasons.append("表格未原样保留")
        return "；".join(reasons)


def _check(reply: str, page_text: str, table_markdowns: Sequence[str], source: str) -> _Problems:
    flat_reply = re.sub(r"\s+", "", reply)
    missing = [
        i for i, t in enumerate(table_markdowns, start=1) if re.sub(r"\s+", "", t) not in flat_reply
    ]
    return _Problems(
        bad_numbers=closure_violations(reply, source),
        lost_numbers=closure_violations(page_text, reply),
        bad_chars=char_violations(reply, source),
        lost_chars=char_violations(page_text, reply),
        missing_tables=missing,
    )


class LlmStructurer:
    def __init__(
        self,
        http: HttpClient,
        *,
        model: str,
        prompt_version: str = "v1",
        max_tokens: int = 4000,
    ) -> None:
        self._http = http
        self.model = model
        self.max_tokens = max_tokens
        self._template = load_prompt(prompt_version, PROMPT_DIR)
        self.usage = Usage()

    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        tables = "\n\n".join(table_markdowns) if table_markdowns else "（本页没有表格）"
        source = page_text + "\n" + "\n".join(table_markdowns)
        values = {"{page_text}": page_text, "{tables}": tables}
        prompt = re.sub(r"\{page_text\}|\{tables\}", lambda m: values[m.group()], self._template)
        messages = [{"role": "user", "content": prompt}]

        reply = self._chat(messages)
        problems = _check(reply, page_text, table_markdowns, source)
        if problems.ok:
            return reply
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": problems.retry_message()},
        ]
        reply = self._chat(messages)
        problems = _check(reply, page_text, table_markdowns, source)
        if problems.bad_numbers:
            raise ClosureFailed(problems.bad_numbers)
        if not problems.ok:
            raise StructureFailed(problems.failure_reason())
        return reply

    def _chat(self, messages: list[dict[str, str]]) -> str:
        raw = self._http.post_json(
            CHAT_ENDPOINT,
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self.max_tokens,
            },
        )
        payload: Any = raw.payload
        usage = (payload.get("usage") or {}) if isinstance(payload, dict) else {}
        self.usage.calls += 1
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise StructureFailed("模型回复缺少 choices")
        choice: dict[str, Any] = choices[0]
        if choice.get("finish_reason") == "length":
            raise StructureFailed(
                f"模型回复被截断（finish_reason=length，max_tokens={self.max_tokens}）"
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise StructureFailed("模型回复没有文本内容（可能被拒答或内容过滤）")
        text = content.strip()
        if not text:
            raise StructureFailed("模型回复为空")
        return text
