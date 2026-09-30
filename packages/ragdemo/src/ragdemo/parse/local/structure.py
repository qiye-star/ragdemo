"""大模型整理一页：只排版，不产生数值。整理结果必须通过数字闭合校验。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ragdemo.adapters.http import HttpClient
from ragdemo.parse.describe_llm import Usage, load_prompt
from ragdemo.parse.local.number_closure import closure_violations

CHAT_ENDPOINT = "/chat/completions"
PROMPT_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "parse_structure"


class ClosureFailed(RuntimeError):
    """整理后的文本含有源文里没有的数字（重试后仍然如此）。"""

    def __init__(self, violations: list[str]) -> None:
        super().__init__(f"数字闭合校验失败：{violations}")
        self.violations = violations


class LlmStructurer:
    def __init__(self, http: HttpClient, *, model: str, prompt_version: str = "v1") -> None:
        self._http = http
        self.model = model
        self._template = load_prompt(prompt_version, PROMPT_DIR)
        self.usage = Usage()

    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        tables = "\n\n".join(table_markdowns) if table_markdowns else "（本页没有表格）"
        source = page_text + "\n" + "\n".join(table_markdowns)
        prompt = self._template.replace("{page_text}", page_text).replace("{tables}", tables)
        messages = [{"role": "user", "content": prompt}]

        reply = self._chat(messages)
        bad = closure_violations(reply, source)
        if not bad:
            return reply
        messages += [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": f"你的输出里出现了输入中没有的数字：{', '.join(bad)}。"
                "请只做排版，不要产生任何新数字，重新输出整理后的 markdown。",
            },
        ]
        reply = self._chat(messages)
        bad = closure_violations(reply, source)
        if bad:
            raise ClosureFailed(bad)
        return reply

    def _chat(self, messages: list[dict[str, str]]) -> str:
        raw = self._http.post_json(
            CHAT_ENDPOINT,
            {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": 4000},
        )
        payload: Any = raw.payload
        usage = payload.get("usage") or {}
        self.usage.calls += 1
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        return str(payload["choices"][0]["message"]["content"]).strip()
