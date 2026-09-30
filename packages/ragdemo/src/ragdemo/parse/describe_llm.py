"""真实表格描述器：经出网代理调国内模型（DeepSeek，阶段 3.1）。

只做一件事——让模型为一张表写**一句只陈述内容**的描述；拦截规则一个字不放宽：
模型的输出仍然要过 `validate_description`（违禁词 + 解读词）。被拦截时把拒绝原因回给模型
让它改写，最多 `max_attempts` 次，仍不合格就抛 `DescriptionRejected`——上层
（`build_chunks_and_descriptions`）会把这份文档记为校验失败，不入块，不放行。

提示词版本化存档在 `agents/prompts/table_describe/<version>.md`（CLAUDE.md §3：改 prompt
必须跑评测）；`prompt_version` 随描述器暴露，写库时可以记下是哪一版生成的。

密钥不在这里：`base_url` 指向回环反向代理（`infra/docker-compose.yml` 的 8086 端口），
Bearer 由代理按 `policy.yaml` 注入 `MODEL_API_KEY`，Python 侧拿不到它（CLAUDE.md §3）。
表格内容（公开披露文件里的表）发往境内模型服务，符合"数据境内存储"约束；用户私有材料
不经过这个描述器之外的任何模型路径——调用方负责不把私有材料送进来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ragdemo.adapters.http import HttpClient
from ragdemo.parse.describe import (
    FORBIDDEN_PATTERNS,
    INTERPRETATION_PATTERNS,
    DescriptionRejected,
    validate_description,
)

PROMPT_DIR = Path(__file__).resolve().parents[1] / "agents" / "prompts" / "table_describe"
CHAT_ENDPOINT = "/chat/completions"
# 表太长时只给模型前若干行（连同总行数），描述只需要主题、维度与行数。
MAX_ROWS_IN_PROMPT = 30
MAX_CHARS_IN_PROMPT = 8000


def load_prompt(version: str, directory: Path = PROMPT_DIR) -> str:
    """读某一版提示词正文（去掉 YAML 头）。"""
    raw = (directory / f"{version}.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n.*?\n---\n", raw, flags=re.DOTALL)
    return raw[match.end() :] if match else raw


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rejected_attempts: int = 0


class LlmTableDescriber:
    def __init__(
        self,
        http: HttpClient,
        *,
        model: str,
        prompt_version: str = "v3",
        max_attempts: int = 3,
        prompt_dir: Path = PROMPT_DIR,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        self._http = http
        self.model = model
        self.prompt_version = prompt_version
        self._max_attempts = max_attempts
        self._template = load_prompt(prompt_version, prompt_dir)
        self.usage = Usage()

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str:
        rows = [r for r in table_markdown.splitlines() if r.strip().startswith("|")]
        if not rows:
            raise DescriptionRejected("不是 Markdown 表格")
        prompt = self._render(table_markdown, rows, title=title, section_path=section_path)
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        last_reason = ""
        for _ in range(self._max_attempts):
            reply = self._chat(messages)
            try:
                return validate_description(_one_sentence(reply))
            except DescriptionRejected as exc:
                last_reason = str(exc)
                self.usage.rejected_attempts += 1
                hits = _offending_words(reply)
                named = "、".join(f"“{w}”" for w in hits) or "被拦截的词"
                messages += [
                    {"role": "assistant", "content": reply},
                    {
                        "role": "user",
                        "content": f"上一句不合格：{last_reason}。其中触发拦截的字样是 {named}。"
                        f"请重写：整句里**不要出现** {named}——表里如果有同名的列，"
                        "就不要提这一列，只写其余列；只陈述表里有什么，不要评价、预测，"
                        "也不要出现买卖 / 评级类字样。只输出一句，不要解释。",
                    },
                ]
        raise DescriptionRejected(f"模型 {self._max_attempts} 次输出都被拦截：{last_reason}")

    # ------------------------------------------------------------------

    def _render(
        self, table_markdown: str, rows: list[str], *, title: str, section_path: str
    ) -> str:
        body = table_markdown
        if len(rows) > MAX_ROWS_IN_PROMPT + 2 or len(body) > MAX_CHARS_IN_PROMPT:
            header, separator, data = rows[0], rows[1] if len(rows) > 1 else "", rows[2:]
            shown = "\n".join([header, separator, *data[:MAX_ROWS_IN_PROMPT]])[:MAX_CHARS_IN_PROMPT]
            note = f"（表格共 {len(data)} 行正文，上面只显示前 {MAX_ROWS_IN_PROMPT} 行）"
            body = f"{shown}\n{note}"
        return (
            self._template.replace("{title}", title)
            .replace("{section_path}", section_path)
            .replace("{table_markdown}", body)
        )

    def _chat(self, messages: list[dict[str, str]]) -> str:
        raw = self._http.post_json(
            CHAT_ENDPOINT,
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 300,
            },
        )
        payload: Any = raw.payload
        try:
            content = str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise DescriptionRejected(
                f"模型响应缺少 choices[0].message.content：{payload!r:.200}"
            ) from exc
        usage = payload.get("usage") or {}
        self.usage.calls += 1
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        return content


def _offending_words(text: str) -> list[str]:
    """回复里实际触发拦截的字样（含紧随其后的一个实词，如"说明列"），回给模型让它避开。"""
    found: list[str] = []
    for pattern in (*FORBIDDEN_PATTERNS, *INTERPRETATION_PATTERNS):
        for m in pattern.finditer(text):
            end = min(len(text), m.end() + 1)
            word = text[m.start() : end].strip("，。；、 ")
            if word and word not in found:
                found.append(word)
    return found


def _one_sentence(reply: str) -> str:
    """模型偶尔会带上引号、前缀或换行——只取第一个非空行，去掉首尾引号。"""
    for line in reply.strip().splitlines():
        text = line.strip().strip("\"“”'")
        if text:
            return text
    return ""
