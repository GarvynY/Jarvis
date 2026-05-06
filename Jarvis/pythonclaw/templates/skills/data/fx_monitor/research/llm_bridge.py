"""
Phase 9 — Shared LLM call helper for all research agents.

Priority order:
  1. DEEPSEEK_API_KEY env var   + openai package      → DeepSeek Chat
  2. pythonclaw.json deepseek.apiKey                 → DeepSeek Chat (config fallback)
  3. ANTHROPIC_API_KEY env var  + anthropic package  → Claude Haiku fallback
  4. None of the above                               → returns ("", {})

All agents import call_llm() from here instead of duplicating the logic.
Adding a new provider only requires touching this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from pythonclaw.core.rate_limit import call_with_backoff
except Exception:  # noqa: BLE001 - skill can run outside installed package.
    def call_with_backoff(provider, func, *args, **kwargs):  # type: ignore[no-redef]
        return func(*args, **kwargs)


# ── Provider constants ────────────────────────────────────────────────────────

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_DEEPSEEK_MODEL  = "deepseek-chat"
_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def _get_deepseek_key() -> str:
    """Return DeepSeek API key from env or pythonclaw.json config."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    # Fallback: read from ~/.pythonclaw/pythonclaw.json
    cfg_path = Path.home() / ".pythonclaw" / "pythonclaw.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        key = (data.get("llm") or {}).get("deepseek", {}).get("apiKey", "")
    except Exception:  # noqa: BLE001
        pass
    return key


def call_llm(
    prompt: str,
    system: str,
    max_tokens: int = 1024,
    timeout: float = 30.0,
) -> tuple[str, dict[str, int]]:
    """
    Make one LLM call (blocking).

    Tries DeepSeek first, then Anthropic, then returns ("", {}).

    Returns:
        (response_text, token_usage_dict)
        token_usage_dict keys: "prompt_tokens", "completion_tokens"
    """
    # ── 1. Try DeepSeek ───────────────────────────────────────────────────────
    deepseek_key = _get_deepseek_key()
    if deepseek_key:
        try:
            import openai  # optional (already installed for DeepSeek)
            client = openai.OpenAI(
                api_key=deepseek_key,
                base_url=_DEEPSEEK_BASE_URL,
                timeout=timeout,
            )
            resp = call_with_backoff(
                "deepseek",
                client.chat.completions.create,
                model=_DEEPSEEK_MODEL,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
            )
            text = resp.choices[0].message.content or ""
            usage_obj = resp.usage
            usage = {
                "prompt_tokens":     usage_obj.prompt_tokens     if usage_obj else 0,
                "completion_tokens": usage_obj.completion_tokens if usage_obj else 0,
            }
            return text, usage
        except Exception:  # noqa: BLE001
            pass

    # ── 2. Try Anthropic fallback ─────────────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            import anthropic  # optional
            client = anthropic.Anthropic(api_key=anthropic_key, timeout=timeout)
            msg = call_with_backoff(
                "anthropic",
                client.messages.create,
                model=_ANTHROPIC_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text: str = msg.content[0].text if msg.content else ""
            usage: dict[str, int] = {
                "prompt_tokens":     msg.usage.input_tokens,
                "completion_tokens": msg.usage.output_tokens,
            }
            return text, usage
        except Exception:  # noqa: BLE001
            pass

    return "", {}
