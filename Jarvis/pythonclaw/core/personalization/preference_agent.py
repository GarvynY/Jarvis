"""PreferenceAgent MVP for news feedback summarization.

This module is intentionally function-based for now.  It takes short-lived
news feedback contexts, calls an LLM with strict JSON output, and stores
pending preference declarations for explicit user confirmation later.

It must not load or send the raw user profile to the LLM.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ... import config
from ..rate_limit import call_with_backoff
from .user_profile_store import (
    create_preference_declaration,
    get_due_news_feedback_contexts,
    mark_news_feedback_contexts_summarized,
)

logger = logging.getLogger(__name__)

MAX_CONTEXTS_PER_RUN = 10
MAX_FEEDBACK_EVENTS_PER_RUN = 20
MAX_DECLARATIONS_PER_RUN = 5
MAX_DECLARATION_CHARS = 120
MAX_PATTERN_CHARS = 120
REPAIR_RETRIES = 2
CONFIDENCE_HINTS = {"low", "medium", "high"}

_SENSITIVE_PATTERNS = (
    re.compile(r"https?://\S+", re.I),
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\+?\d[\d\s().-]{6,}\d)\b"),
    re.compile(r"\b(?:api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*\S+", re.I),
    re.compile(r"银行账号|银行账户|护照|身份证|银行卡|余额"),
)


@dataclass(slots=True)
class PreferenceAgentResult:
    ok: bool = False
    declarations_created: int = 0
    context_ids_summarized: list[str] = field(default_factory=list)
    attempts: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
    trigger_types: list[str] = field(default_factory=list)


LLMCall = Callable[[str, str, int], tuple[str, dict[str, int]]]


def _filter_sensitive_text(value: Any, max_chars: int = 500) -> str:
    text = str(value or "").strip()
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[已过滤]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _extract_json_text(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text or "", re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text or ""


def _load_json_object(text: str) -> dict[str, Any]:
    data = json.loads(_extract_json_text(text))
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object")
    return data


def _safe_context_for_prompt(context: dict[str, Any]) -> dict[str, Any]:
    feedback_events = list(context.get("feedback_events") or [])[:MAX_FEEDBACK_EVENTS_PER_RUN]
    articles = list(context.get("articles") or [])[:5]
    return {
        "context_id": str(context.get("id") or ""),
        "trigger_type": _filter_sensitive_text(context.get("trigger_type"), 20),
        "tags": [
            _filter_sensitive_text(tag, 32)
            for tag in list(context.get("tags") or [])[:5]
        ],
        "articles": [
            {
                "title": _filter_sensitive_text(article.get("title"), 160),
                "summary": _filter_sensitive_text(article.get("summary"), 220),
                "published": _filter_sensitive_text(article.get("published"), 40),
                "tags": [
                    _filter_sensitive_text(tag, 32)
                    for tag in list(article.get("tags") or [])[:5]
                ],
            }
            for article in articles
            if isinstance(article, dict)
        ],
        "feedback_events": [
            {
                "event_type": _filter_sensitive_text(event.get("event_type"), 24),
                "topic": _filter_sensitive_text(event.get("topic"), 48),
                "category": _filter_sensitive_text(event.get("category"), 48),
            }
            for event in feedback_events
            if isinstance(event, dict)
        ],
    }


def _build_preference_prompt(contexts: list[dict[str, Any]]) -> str:
    safe_contexts = [_safe_context_for_prompt(ctx) for ctx in contexts[:MAX_CONTEXTS_PER_RUN]]
    payload = json.dumps({"contexts": safe_contexts}, ensure_ascii=False, indent=2)
    return (
        "你是 Jarvis 的 PreferenceAgent MVP。你的任务是根据用户对新闻推送的反馈，"
        "归纳待用户确认的隐式偏好声明。\n\n"
        "重要限制：\n"
        "- 只基于输入中的新闻标题、摘要、标签和反馈事件判断。\n"
        "- 不要推断身份、财务状况、健康、政治立场、地址、账号等敏感信息。\n"
        "- 不要把单次偶然反馈写成确定偏好。\n"
        "- 如果样本少或由 expired 触发，声明要更保守，confidence_hint 优先 low。\n"
        "- 只输出 JSON object，不要 Markdown，不要解释。\n\n"
        "输出 Schema：\n"
        '{"declarations":[{"declaration":"不超过120字的中文偏好声明","confidence_hint":"low|medium|high","evidence_count":3,"source_context_ids":["1"]}],'
        '"rejected_patterns":[{"pattern":"不超过120字的反例或质量问题","reason":"old|shallow|irrelevant|known|mixed"}]}\n\n'
        f"最多输出 {MAX_DECLARATIONS_PER_RUN} 条 declarations。"
        "如果没有足够证据，declarations 返回空数组。\n\n"
        f"输入数据：\n{payload}"
    )


def _build_repair_prompt(original_prompt: str, bad_response: str, error: Exception) -> str:
    clipped = _filter_sensitive_text(bad_response, 3000) or "（空响应）"
    return (
        f"{original_prompt}\n\n"
        "上一次输出未通过 JSON/Schema 校验。\n"
        f"错误：{type(error).__name__}: {error}\n"
        f"上一次输出：{clipped}\n\n"
        "请只返回修正后的 JSON object，必须包含 declarations 和 rejected_patterns。"
    )


def _validate_preference_payload(data: dict[str, Any], valid_context_ids: set[str]) -> dict[str, Any]:
    if not isinstance(data.get("declarations"), list):
        raise ValueError("declarations must be a list")
    if not isinstance(data.get("rejected_patterns"), list):
        raise ValueError("rejected_patterns must be a list")

    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data["declarations"][:MAX_DECLARATIONS_PER_RUN]:
        if not isinstance(item, dict):
            raise ValueError("each declaration must be an object")
        declaration = _filter_sensitive_text(item.get("declaration"), MAX_DECLARATION_CHARS)
        if not declaration:
            continue
        key = declaration.lower()
        if key in seen:
            continue
        seen.add(key)
        confidence_hint = str(item.get("confidence_hint") or "low").strip().lower()
        if confidence_hint not in CONFIDENCE_HINTS:
            raise ValueError(f"invalid confidence_hint: {confidence_hint}")
        try:
            evidence_count = max(0, int(item.get("evidence_count") or 0))
        except (TypeError, ValueError):
            raise ValueError("evidence_count must be an integer") from None
        source_context_ids = [
            str(cid)
            for cid in list(item.get("source_context_ids") or [])[:MAX_CONTEXTS_PER_RUN]
            if str(cid) in valid_context_ids
        ]
        if not source_context_ids:
            raise ValueError("declaration missing valid source_context_ids")
        declarations.append(
            {
                "declaration": declaration,
                "confidence_hint": confidence_hint,
                "evidence_count": evidence_count,
                "source_context_ids": source_context_ids,
            }
        )

    rejected_patterns: list[dict[str, str]] = []
    for item in data["rejected_patterns"][:MAX_DECLARATIONS_PER_RUN]:
        if not isinstance(item, dict):
            continue
        pattern = _filter_sensitive_text(item.get("pattern"), MAX_PATTERN_CHARS)
        reason = _filter_sensitive_text(item.get("reason"), 32)
        if pattern:
            rejected_patterns.append({"pattern": pattern, "reason": reason or "mixed"})

    return {"declarations": declarations, "rejected_patterns": rejected_patterns}


def _call_default_llm(prompt: str, system: str, max_tokens: int) -> tuple[str, dict[str, int]]:
    from openai import OpenAI

    provider = config.get_str("llm", "provider", env="LLM_PROVIDER", default="deepseek")
    api_key = config.get_str("llm", provider, "apiKey", env=f"{provider.upper()}_API_KEY")
    base_url = config.get_str(
        "llm", provider, "baseUrl",
        default="https://api.deepseek.com/v1" if provider == "deepseek" else "",
    )
    model = config.get_str(
        "llm", provider, "model",
        default="deepseek-chat" if provider == "deepseek" else "deepseek-chat",
    )
    if not api_key:
        raise RuntimeError(f"LLM API key not configured for provider: {provider}")
    client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=45.0)
    response = call_with_backoff(
        provider,
        client.chat.completions.create,
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    usage = getattr(response, "usage", None)
    token_usage = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    return response.choices[0].message.content.strip(), token_usage


def _run_json_llm_with_repair(
    prompt: str,
    *,
    valid_context_ids: set[str],
    llm_call: LLMCall | None = None,
) -> tuple[dict[str, Any], int, dict[str, int]]:
    call = llm_call or _call_default_llm
    system = (
        "You are a strict JSON preference summarizer. Return only one JSON object. "
        "Do not include raw personal data or unsupported inferences."
    )
    current_prompt = prompt
    last_text = ""
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    last_error: Exception | None = None
    for attempt in range(REPAIR_RETRIES + 1):
        text, usage = call(current_prompt, system, 900)
        last_text = text or ""
        for key in token_usage:
            token_usage[key] += int((usage or {}).get(key, 0) or 0)
        try:
            data = _load_json_object(last_text)
            validated = _validate_preference_payload(data, valid_context_ids)
            return validated, attempt + 1, token_usage
        except Exception as exc:  # noqa: BLE001 - repair loop intentionally catches validation drift
            last_error = exc
            if attempt >= REPAIR_RETRIES:
                break
            current_prompt = _build_repair_prompt(prompt, last_text, exc)
    raise ValueError(f"PreferenceAgent JSON validation failed: {last_error}")


def run_preference_agent_for_user(
    telegram_user_id: int | str,
    *,
    db_path: str | None = None,
    llm_call: LLMCall | None = None,
) -> PreferenceAgentResult:
    """Summarize due news feedback contexts into pending preference declarations."""
    contexts = get_due_news_feedback_contexts(telegram_user_id, db_path=db_path)
    if not contexts:
        return PreferenceAgentResult(ok=True)
    contexts = contexts[:MAX_CONTEXTS_PER_RUN]
    context_ids = [str(ctx.get("id")) for ctx in contexts if ctx.get("id") is not None]
    valid_context_ids = set(context_ids)
    prompt = _build_preference_prompt(contexts)
    trigger_types = sorted({
        str(ctx.get("trigger_type") or "")
        for ctx in contexts
        if ctx.get("trigger_type")
    })

    try:
        payload, attempts, token_usage = _run_json_llm_with_repair(
            prompt,
            valid_context_ids=valid_context_ids,
            llm_call=llm_call,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PreferenceAgent failed for user_id=%s: %s", telegram_user_id, exc)
        return PreferenceAgentResult(
            ok=False,
            attempts=REPAIR_RETRIES + 1,
            error=str(exc),
            trigger_types=trigger_types,
        )

    created = 0
    rejected_patterns = payload.get("rejected_patterns") or []
    for item in payload.get("declarations") or []:
        metadata = {
            "confidence_hint": item["confidence_hint"],
            "source_context_ids": item["source_context_ids"],
            "trigger_types": trigger_types,
            "rejected_patterns": rejected_patterns,
        }
        create_preference_declaration(
            telegram_user_id,
            item["declaration"],
            evidence_count=item["evidence_count"],
            source="news_feedback",
            metadata=metadata,
            status="pending",
            db_path=db_path,
        )
        created += 1

    if created:
        mark_news_feedback_contexts_summarized(context_ids, db_path=db_path)

    return PreferenceAgentResult(
        ok=True,
        declarations_created=created,
        context_ids_summarized=context_ids if created else [],
        attempts=attempts,
        token_usage=token_usage,
        trigger_types=trigger_types,
    )

