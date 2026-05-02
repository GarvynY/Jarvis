#!/usr/bin/env python3
"""
Phase 9 Step 3b — NewsAgent standalone tests.

Uses example news cache data — no network, no actual LLM calls.
All external I/O (_read_news_cache, _call_llm) is mocked.

Tests:
  1. test_cache_missing       — _read_news_cache returns error → partial
  2. test_cache_empty         — cache present but empty → partial
  3. test_llm_ok              — cache + LLM JSON → ok, findings, sources, token_usage
  4. test_llm_fallback        — LLM returns "" → heuristic fallback, partial
  5. test_sourceref_fields    — all SourceRef fields populated correctly
  6. test_conflict_risk       — mixed bullish+bearish in LLM output → risk
  7. test_json_safe           — output passes JSON-safety check
  8. test_no_banned_terms     — no preset banned terms in any text field
  9. test_banned_terms_sanitized — LLM banned terms are removed

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research/agents
    python test_news_agent.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest.mock
from pathlib import Path

_HERE         = Path(__file__).parent
_RESEARCH_DIR = _HERE.parent

if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from schema import ResearchTask, SafeUserContext, AgentOutput, FX_CNYAUD_PRESET  # noqa: E402
from agents.news_agent import (  # noqa: E402
    NewsAgent, _build_news_output, _collect_and_analyse, _NEWS_CACHE_FILE,
)


# ── Example cache data ────────────────────────────────────────────────────────
#
# This matches the format written by news_monitor._save_recent_cache():
#   { "articles": [...], "updated_at": "..." }

_EXAMPLE_CACHE_UPDATED_AT = "2026-05-02T10:00:00Z"

_EXAMPLE_ARTICLES = [
    {
        "title": "RBA signals possible rate hike amid persistent inflation",
        "url": "https://example.com/news/1",
        "published": "Fri, 02 May 2026 08:00:00 +0000",
        "snippet": "Reserve Bank of Australia governor hints at a hawkish stance as inflation remains above target.",
        "keyword": "RBA interest rate decision",
    },
    {
        "title": "Australia faces recession risk as trade war escalates with US",
        "url": "https://example.com/news/2",
        "published": "Fri, 02 May 2026 09:30:00 +0000",
        "snippet": "Economists warn of slowdown risk as tariff disputes weigh on Australian exports.",
        "keyword": "China Australia trade",
    },
    {
        "title": "Iron ore rally supports AUD as China demand recovers",
        "url": "https://example.com/news/3",
        "published": "Thu, 01 May 2026 14:00:00 +0000",
        "snippet": "Iron ore prices hit three-month high boosted by China demand.",
        "keyword": "iron ore price Australia",
    },
    {
        "title": "China GDP growth beats expectations in Q1 2026",
        "url": "https://example.com/news/4",
        "published": "Thu, 01 May 2026 06:00:00 +0000",
        "snippet": "China economy expanded 5.2% year-on-year, above forecast.",
        "keyword": "China economy GDP",
    },
]

# LLM JSON response for the above articles
_MOCK_LLM_JSON = json.dumps({
    "summary": "RBA鹰派信号与贸易战担忧并存，铁矿石上涨支撑AUD",
    "findings": [
        {"index": 1, "direction": "bullish_aud", "reason": "RBA加息预期支撑澳元"},
        {"index": 2, "direction": "bearish_aud", "reason": "贸易战升级压制AUD"},
        {"index": 3, "direction": "bullish_aud", "reason": "铁矿石上涨利好AUD"},
        {"index": 4, "direction": "neutral",     "reason": "中国GDP数据为中性信号"},
    ],
    "overall_direction": "mixed",
    "risks": ["新闻信号方向存在矛盾，多空力量均衡"],
    "uncertainty_notes": "信号混杂，方向不明",
}, ensure_ascii=False)

_MOCK_TOKEN_USAGE = {"prompt_tokens": 312, "completion_tokens": 187}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_task() -> ResearchTask:
    return ResearchTask.from_preset(
        FX_CNYAUD_PRESET,
        safe_user_context=SafeUserContext(purpose="tuition"),
        focus_assets=["AUD", "CNY"],
        focus_pair="CNY/AUD",
    )


def _mock_cache_ok(articles: list | None = None):
    import agents.news_agent as _mod
    items = articles if articles is not None else _EXAMPLE_ARTICLES
    return unittest.mock.patch.object(
        _mod, "_read_news_cache",
        return_value=(items, None, _EXAMPLE_CACHE_UPDATED_AT),
    )


def _mock_cache_missing():
    import agents.news_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_read_news_cache",
        return_value=([], "news_cache_file_missing", ""),
    )


def _mock_cache_empty():
    import agents.news_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_read_news_cache",
        return_value=([], "news_cache_empty", _EXAMPLE_CACHE_UPDATED_AT),
    )


def _mock_llm_ok():
    import agents.news_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_call_llm",
        return_value=(_MOCK_LLM_JSON, _MOCK_TOKEN_USAGE),
    )


def _mock_llm_fail():
    import agents.news_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_call_llm",
        return_value=("", {}),
    )


def _mock_llm_text(text: str):
    import agents.news_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_call_llm",
        return_value=(text, _MOCK_TOKEN_USAGE),
    )


def _print_output(output: AgentOutput) -> None:
    print(f"   status={output.status}  conf={output.confidence:.2f}  "
          f"latency={output.latency_ms}ms  findings={len(output.findings)}")
    print(f"   summary: {output.summary[:70]}")
    for f in output.findings[:4]:
        print(f"   [{f.key:10s}] [{f.direction or 'none':14s}] {f.summary[:55]}")
    if output.risks:
        print(f"   risks: {output.risks[0][:70]}")
    if output.missing_data:
        print(f"   missing: {output.missing_data}")
    if output.token_usage:
        print(f"   token_usage: {output.token_usage}")


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_cache_missing() -> None:
    """Cache file not found → status=partial, missing_data explains."""
    with _mock_cache_missing():
        output = await NewsAgent().run(_make_task())

    assert output.status == "partial", f"Expected partial, got {output.status}"
    assert any("news_cache" in m for m in output.missing_data), (
        f"Expected news_cache in missing_data, got: {output.missing_data}"
    )
    assert output.agent_name == "news_agent"
    json.dumps(output.to_dict(), ensure_ascii=False)

    print("\n-- test_cache_missing")
    _print_output(output)
    print("   PASS")


async def test_cache_empty() -> None:
    """Cache exists but no articles → status=partial."""
    with _mock_cache_empty():
        output = await NewsAgent().run(_make_task())

    assert output.status == "partial"
    assert any("news_cache" in m for m in output.missing_data)

    print("\n-- test_cache_empty")
    _print_output(output)
    print("   PASS")


async def test_llm_ok() -> None:
    """Cache present + LLM returns JSON → ok, findings, sources, token_usage."""
    with _mock_cache_ok(), _mock_llm_ok():
        output = await NewsAgent().run(_make_task())

    assert output.agent_name == "news_agent"
    assert output.status in ("ok", "partial"), f"Expected ok/partial, got {output.status}"
    assert len(output.findings) >= 3, f"Expected ≥3 findings, got {len(output.findings)}"
    assert len(output.sources) > 0, "Expected sources from articles"
    assert output.confidence > 0
    assert output.token_usage.get("prompt_tokens", 0) > 0, (
        f"Expected token_usage with prompt_tokens, got: {output.token_usage}"
    )
    # LLM summary should be used
    assert "RBA" in output.summary or len(output.summary) > 10, (
        f"Expected non-trivial summary, got: {output.summary}"
    )
    json.dumps(output.to_dict(), ensure_ascii=False)

    print("\n-- test_llm_ok")
    _print_output(output)
    print("   PASS")


async def test_llm_fallback() -> None:
    """LLM unavailable → heuristic fallback, status=partial, token_usage estimated."""
    with _mock_cache_ok(), _mock_llm_fail():
        output = await NewsAgent().run(_make_task())

    assert output.status == "partial", f"Expected partial (LLM fallback), got {output.status}"
    assert any("llm_unavailable" in m for m in output.missing_data), (
        f"Expected llm_unavailable in missing_data, got: {output.missing_data}"
    )
    assert len(output.findings) > 0, "Should still have heuristic findings"
    # Estimated token count should be present
    assert "prompt_tokens" in output.token_usage, (
        f"Expected estimated prompt_tokens, got: {output.token_usage}"
    )

    print("\n-- test_llm_fallback")
    _print_output(output)
    print("   PASS")


async def test_sourceref_fields() -> None:
    """All required SourceRef fields are populated."""
    with _mock_cache_ok(), _mock_llm_ok():
        output = await NewsAgent().run(_make_task())

    assert len(output.sources) > 0, "Expected at least one source"
    for src in output.sources:
        assert src.title,        f"SourceRef missing title: {src}"
        assert src.url,          f"SourceRef missing url: {src}"
        assert src.source,       f"SourceRef missing source: {src}"
        assert src.retrieved_at, f"SourceRef missing retrieved_at: {src}"
        # source must be google_news_rss for cached news
        assert src.source == "google_news_rss", f"Unexpected source: {src.source}"

    print("\n-- test_sourceref_fields")
    print(f"   {len(output.sources)} sources, all fields present")
    for s in output.sources[:2]:
        print(f"   title={s.title[:40]}  published_at={s.published_at}")
    print("   PASS")


async def test_conflict_risk() -> None:
    """LLM output with mixed directions → conflict risk in output.risks."""
    # _MOCK_LLM_JSON has bullish_aud + bearish_aud findings
    with _mock_cache_ok(), _mock_llm_ok():
        output = await NewsAgent().run(_make_task())

    bullish = sum(1 for f in output.findings if f.direction == "bullish_aud")
    bearish = sum(1 for f in output.findings if f.direction == "bearish_aud")

    if bullish > 0 and bearish > 0:
        assert any("矛盾" in r or "conflict" in r.lower() for r in output.risks), (
            f"Expected conflict risk when bullish={bullish} bearish={bearish}. "
            f"Got risks: {output.risks}"
        )

    print("\n-- test_conflict_risk")
    print(f"   bullish={bullish}  bearish={bearish}  risks={output.risks}")
    print("   PASS")


async def test_json_safe() -> None:
    """Output serialises to valid JSON in all three paths."""
    with _mock_cache_missing():
        out1 = await NewsAgent().run(_make_task())
    json.dumps(out1.to_dict(), ensure_ascii=False)

    with _mock_cache_ok(), _mock_llm_ok():
        out2 = await NewsAgent().run(_make_task())
    json.dumps(out2.to_dict(), ensure_ascii=False)

    with _mock_cache_ok(), _mock_llm_fail():
        out3 = await NewsAgent().run(_make_task())
    json.dumps(out3.to_dict(), ensure_ascii=False)

    print("\n-- test_json_safe")
    print(f"   cache_missing: {len(json.dumps(out1.to_dict()))} chars")
    print(f"   llm_ok:        {len(json.dumps(out2.to_dict()))} chars")
    print(f"   llm_fallback:  {len(json.dumps(out3.to_dict()))} chars")
    print("   PASS")


async def test_no_banned_terms() -> None:
    """No FX_CNYAUD_PRESET banned terms appear in any output text."""
    with _mock_cache_ok(), _mock_llm_ok():
        output = await NewsAgent().run(_make_task())

    all_text = (
        output.summary
        + " ".join(f.summary for f in output.findings)
        + " ".join(output.risks)
    )
    for term in FX_CNYAUD_PRESET.banned_terms:
        assert term not in all_text, f"Banned term {term!r} found in output"

    print("\n-- test_no_banned_terms")
    print("   No banned terms found")
    print("   PASS")


async def test_banned_terms_sanitized() -> None:
    """LLM banned terms in summary/risks are sanitized before output."""
    unsafe = json.dumps({
        "summary": "建议买入 AUD，立即操作",
        "findings": [{"index": 1, "direction": "bullish_aud", "reason": "x"}],
        "overall_direction": "bullish_aud",
        "risks": ["最佳时机这类说法不应保留"],
    }, ensure_ascii=False)
    with _mock_cache_ok(), _mock_llm_text(unsafe):
        output = await NewsAgent().run(_make_task())

    all_text = output.summary + " ".join(output.risks)
    for term in FX_CNYAUD_PRESET.banned_terms:
        assert term not in all_text, f"Banned term {term!r} found in output"
    assert "unsafe_llm_terms_removed" in output.missing_data

    print("\n-- test_banned_terms_sanitized")
    print(f"   summary: {output.summary}")
    print("   PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 3b -- NewsAgent tests (mocked cache + LLM)")
    print(f"Cache path: {_NEWS_CACHE_FILE}")
    print("=" * 60)

    await test_cache_missing()
    await test_cache_empty()
    await test_llm_ok()
    await test_llm_fallback()
    await test_sourceref_fields()
    await test_conflict_risk()
    await test_json_safe()
    await test_no_banned_terms()
    await test_banned_terms_sanitized()

    print("\n" + "=" * 60)
    print("All 9 tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        NewsAgent.close_executor()
