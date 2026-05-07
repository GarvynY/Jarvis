"""
Shared structured-output helper for LLM calls.

Use this when an agent needs a strict JSON object from an LLM.  The helper
handles common model drift: markdown fences, pre/post text, missing required
keys, and one or more repair calls with a focused prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

LLMCall = Callable[[str, str, int], tuple[str, dict[str, int]]]
JSONValidator = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class StructuredLLMResult:
    text: str = ""
    data: dict[str, Any] | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    attempts: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.data is not None


def extract_json_text(text: str) -> str:
    """Extract the first JSON object from possibly-decorated LLM output."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text or "", re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text or ""


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse an LLM response into a JSON object, raising on invalid shape."""
    data = json.loads(extract_json_text(text))
    if not isinstance(data, dict):
        raise TypeError("LLM response must be a JSON object")
    return data


def require_keys(data: dict[str, Any], keys: list[str] | tuple[str, ...]) -> None:
    """Validate that required top-level keys are present."""
    missing = [key for key in keys if key not in data]
    if missing:
        raise KeyError(f"missing required keys: {', '.join(missing)}")


def build_repair_prompt(
    original_prompt: str,
    bad_response: str,
    error: Exception,
    *,
    required_keys: list[str] | tuple[str, ...] = (),
    schema_hint: str = "",
) -> str:
    """Build a focused repair prompt after structured-output failure."""
    clipped = bad_response[:4000] if bad_response else "（空响应）"
    required = ", ".join(required_keys) if required_keys else "原任务要求的字段"
    hint = f"\n\n目标 JSON 结构提示：\n{schema_hint}" if schema_hint else ""
    return (
        f"{original_prompt}\n\n"
        "━━━ 上一次输出解析失败 ━━━\n"
        f"解析错误：{type(error).__name__}: {error}\n\n"
        "上一次模型输出如下，请修复为严格合法 JSON：\n"
        f"{clipped}\n\n"
        "修复要求：\n"
        "1. 只返回一个 JSON object，不要 markdown，不要解释。\n"
        f"2. 必须保留 required keys: {required}。\n"
        "3. 不要新增原任务未要求的事实、来源或结论。"
        f"{hint}"
    )


def call_json_with_repair(
    call_llm: LLMCall,
    prompt: str,
    system: str,
    *,
    max_tokens: int,
    required_keys: list[str] | tuple[str, ...] = (),
    validate: JSONValidator | None = None,
    repair_retries: int = 2,
    schema_hint: str = "",
) -> StructuredLLMResult:
    """Call an LLM until a valid JSON object is returned or retries are exhausted."""
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    current_prompt = prompt
    last_text = ""
    last_error: Exception | None = None

    for attempt in range(repair_retries + 1):
        text, usage = call_llm(current_prompt, system, max_tokens)
        last_text = text or ""
        for key in ("prompt_tokens", "completion_tokens"):
            total_usage[key] += int((usage or {}).get(key, 0) or 0)

        try:
            if not last_text:
                raise ValueError("LLM returned empty response")
            data = parse_json_object(last_text)
            require_keys(data, required_keys)
            if validate:
                validate(data)
            return StructuredLLMResult(
                text=last_text,
                data=data,
                token_usage=total_usage,
                attempts=attempt + 1,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= repair_retries:
                break
            current_prompt = build_repair_prompt(
                prompt,
                last_text,
                exc,
                required_keys=required_keys,
                schema_hint=schema_hint,
            )

    return StructuredLLMResult(
        text=last_text,
        data=None,
        token_usage=total_usage,
        attempts=repair_retries + 1,
        error=f"{type(last_error).__name__}: {last_error}" if last_error else "",
    )
