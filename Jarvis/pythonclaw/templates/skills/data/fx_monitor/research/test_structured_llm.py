"""
Tests for shared structured LLM output repair.

Run:
    python test_structured_llm.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from structured_llm import call_json_with_repair, parse_json_object  # noqa: E402


def test_extracts_fenced_json() -> None:
    data = parse_json_object('```json\n{"summary": "ok"}\n```')
    assert data == {"summary": "ok"}
    print("-- test_extracts_fenced_json PASS")


def test_repairs_missing_key() -> None:
    calls: list[str] = []

    def fake_llm(prompt: str, system: str, max_tokens: int) -> tuple[str, dict[str, int]]:
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"summary": "first"}, ensure_ascii=False), {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }
        return json.dumps({"summary": "fixed", "findings": []}, ensure_ascii=False), {
            "prompt_tokens": 12,
            "completion_tokens": 6,
        }

    result = call_json_with_repair(
        fake_llm,
        "Return JSON",
        "system",
        max_tokens=200,
        required_keys=("summary", "findings"),
        repair_retries=2,
    )

    assert result.ok
    assert result.data == {"summary": "fixed", "findings": []}
    assert result.attempts == 2
    assert result.token_usage == {"prompt_tokens": 22, "completion_tokens": 11}
    assert "上一次输出解析失败" in calls[1]
    print("-- test_repairs_missing_key PASS")


def test_validator_failure_retries() -> None:
    def validate(data: dict[str, Any]) -> None:
        if not isinstance(data.get("sections"), list):
            raise TypeError("sections must be list")

    responses = [
        json.dumps({"sections": "bad"}, ensure_ascii=False),
        json.dumps({"sections": []}, ensure_ascii=False),
    ]

    def fake_llm(prompt: str, system: str, max_tokens: int) -> tuple[str, dict[str, int]]:
        return responses.pop(0), {"prompt_tokens": 1, "completion_tokens": 1}

    result = call_json_with_repair(
        fake_llm,
        "Return sections",
        "system",
        max_tokens=200,
        required_keys=("sections",),
        validate=validate,
        repair_retries=1,
    )

    assert result.ok
    assert result.data == {"sections": []}
    assert result.attempts == 2
    print("-- test_validator_failure_retries PASS")


def test_exhausted_retries() -> None:
    def fake_llm(prompt: str, system: str, max_tokens: int) -> tuple[str, dict[str, int]]:
        return "not json", {"prompt_tokens": 1, "completion_tokens": 0}

    result = call_json_with_repair(
        fake_llm,
        "Return JSON",
        "system",
        max_tokens=200,
        required_keys=("summary",),
        repair_retries=1,
    )

    assert not result.ok
    assert result.attempts == 2
    assert "JSONDecodeError" in result.error
    print("-- test_exhausted_retries PASS")


if __name__ == "__main__":
    print("Structured LLM tests")
    print("====================")
    test_extracts_fenced_json()
    test_repairs_missing_key()
    test_validator_failure_retries()
    test_exhausted_retries()
    print("====================")
    print("All structured LLM tests passed.")
