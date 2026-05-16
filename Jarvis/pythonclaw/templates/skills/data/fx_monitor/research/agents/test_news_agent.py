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
import os
import sys
import unittest.mock
from pathlib import Path

_HERE         = Path(__file__).parent
_RESEARCH_DIR = _HERE.parent
_FX_MONITOR_DIR = _RESEARCH_DIR.parent

if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))
if str(_FX_MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(_FX_MONITOR_DIR))

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


def _mock_monitor_refresh(articles: list | None = None, error: str | None = None):
    import agents.news_agent as _mod
    items = articles if articles is not None else _EXAMPLE_ARTICLES
    return unittest.mock.patch.object(
        _mod,
        "_refresh_news_via_monitor",
        return_value=(items, error, "2026-05-06T10:00:00Z"),
    )


def _mock_cache_not_mocked():
    import agents.news_agent as _mod
    return unittest.mock.patch.object(
        _mod,
        "_cache_reader_is_mocked",
        return_value=False,
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


async def test_stale_cache_refreshes() -> None:
    """Stale cache triggers a no-mark-seen refresh before LLM analysis."""
    stale_articles = [
        {
            **_EXAMPLE_ARTICLES[0],
            "published": "Wed, 29 Apr 2026 12:00:00 +0000",
        }
    ]
    fresh_articles = [
        {
            **_EXAMPLE_ARTICLES[2],
            "published": "Wed, 06 May 2026 09:30:00 +0000",
        }
    ]
    with _mock_cache_not_mocked(), _mock_cache_ok(stale_articles), \
         _mock_monitor_refresh(fresh_articles), _mock_llm_ok():
        raw = _collect_and_analyse()

    assert raw["articles"] == fresh_articles
    assert raw["updated_at"] == "2026-05-06T10:00:00Z"

    print("\n-- test_stale_cache_refreshes")
    print(f"   refreshed title: {raw['articles'][0]['title'][:60]}")
    print("   PASS")


async def test_stale_cache_no_recent_news() -> None:
    """Stale cache + empty refresh returns no-news partial, not old headlines."""
    stale_articles = [
        {
            **_EXAMPLE_ARTICLES[0],
            "published": "Wed, 29 Apr 2026 12:00:00 +0000",
        }
    ]
    with _mock_cache_not_mocked(), _mock_cache_ok(stale_articles), \
         _mock_monitor_refresh([], "news_monitor_refresh_empty"):
        output = await NewsAgent().run(_make_task())

    assert output.status == "partial"
    assert not output.findings
    assert "暂无近期相关新闻" in output.summary
    assert any("news_monitor_refresh_empty" in m for m in output.missing_data)

    print("\n-- test_stale_cache_no_recent_news")
    _print_output(output)
    print("   PASS")


# ── Phase 10.5.2 — research vs notify mode tests ────────────────────────────

async def test_research_mode_returns_all_despite_seen() -> None:
    """Research mode (ignore_seen=True) returns articles even when all URLs are in seen_urls."""
    from news_monitor import check_news, _load_state, _save_state, STATE_FILE
    import tempfile, shutil

    backup = None
    if os.path.exists(STATE_FILE):
        backup = STATE_FILE + ".bak"
        shutil.copy2(STATE_FILE, backup)

    try:
        fake_urls = ["https://example.com/seen-1", "https://example.com/seen-2"]
        _save_state({"seen_urls": fake_urls})

        fake_articles = [
            {"title": "A", "url": "https://example.com/seen-1", "published": "", "snippet": ""},
            {"title": "B", "url": "https://example.com/seen-2", "published": "", "snippet": ""},
            {"title": "C", "url": "https://example.com/new-3", "published": "", "snippet": ""},
        ]

        import news_monitor as _nm
        with unittest.mock.patch.object(_nm, "_fetch_google_news_rss", return_value=fake_articles):
            result = check_news(
                keywords=["test"], mark_seen=False, ignore_seen=True,
            )

        assert "all_articles" in result, "Research mode must return all_articles"
        assert result["total_all"] == 3, f"Expected 3 all_articles, got {result['total_all']}"
        assert result["total_new"] == 1, f"Expected 1 new article, got {result['total_new']}"
    finally:
        if backup:
            shutil.move(backup, STATE_FILE)
        elif os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    print("\n-- test_research_mode_returns_all_despite_seen")
    print(f"   all_articles={result['total_all']}  new_articles={result['total_new']}")
    print("   PASS")


async def test_research_mode_does_not_modify_seen_urls() -> None:
    """Research mode (mark_seen=False) must NOT add any URLs to seen_urls."""
    from news_monitor import check_news, _load_state, _save_state, STATE_FILE
    import shutil

    backup = None
    if os.path.exists(STATE_FILE):
        backup = STATE_FILE + ".bak"
        shutil.copy2(STATE_FILE, backup)

    try:
        original_seen = ["https://example.com/old-1"]
        _save_state({"seen_urls": original_seen})

        fake_articles = [
            {"title": "New", "url": "https://example.com/brand-new", "published": "", "snippet": ""},
        ]

        import news_monitor as _nm
        with unittest.mock.patch.object(_nm, "_fetch_google_news_rss", return_value=fake_articles):
            check_news(keywords=["test"], mark_seen=False, ignore_seen=True)

        state_after = _load_state()
        assert "https://example.com/brand-new" not in state_after["seen_urls"], \
            "Research mode must not add URLs to seen_urls"
        assert state_after["seen_urls"] == original_seen, \
            f"seen_urls was modified: {state_after['seen_urls']}"
    finally:
        if backup:
            shutil.move(backup, STATE_FILE)
        elif os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    print("\n-- test_research_mode_does_not_modify_seen_urls")
    print("   seen_urls unchanged after research mode call")
    print("   PASS")


async def test_research_mode_updates_cache() -> None:
    """Research mode always writes cache even if all articles were previously seen."""
    from news_monitor import check_news, _save_state, _load_recent_cache, \
        STATE_FILE, RECENT_CACHE_FILE
    import shutil

    state_backup = None
    cache_backup = None
    if os.path.exists(STATE_FILE):
        state_backup = STATE_FILE + ".bak"
        shutil.copy2(STATE_FILE, state_backup)
    if os.path.exists(RECENT_CACHE_FILE):
        cache_backup = RECENT_CACHE_FILE + ".bak"
        shutil.copy2(RECENT_CACHE_FILE, cache_backup)

    try:
        _save_state({"seen_urls": ["https://example.com/s1"]})
        if os.path.exists(RECENT_CACHE_FILE):
            os.remove(RECENT_CACHE_FILE)

        fake_articles = [
            {"title": "Seen", "url": "https://example.com/s1", "published": "", "snippet": ""},
        ]

        import news_monitor as _nm
        with unittest.mock.patch.object(_nm, "_fetch_google_news_rss", return_value=fake_articles):
            check_news(keywords=["test"], mark_seen=False, ignore_seen=True)

        cached = _load_recent_cache()
        assert len(cached) == 1, f"Expected 1 cached article, got {len(cached)}"
        assert cached[0]["title"] == "Seen"

        with open(RECENT_CACHE_FILE, encoding="utf-8") as f:
            cache_data = json.load(f)
        assert "updated_at" in cache_data, "Cache must have updated_at timestamp"
    finally:
        if state_backup:
            shutil.move(state_backup, STATE_FILE)
        elif os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        if cache_backup:
            shutil.move(cache_backup, RECENT_CACHE_FILE)
        elif os.path.exists(RECENT_CACHE_FILE):
            os.remove(RECENT_CACHE_FILE)

    print("\n-- test_research_mode_updates_cache")
    print(f"   cache written with {len(cached)} article(s), updated_at present")
    print("   PASS")


async def test_notify_mode_still_filters_seen() -> None:
    """Notify mode (default) still filters seen URLs and marks new ones."""
    from news_monitor import check_news, _load_state, _save_state, STATE_FILE
    import shutil

    backup = None
    if os.path.exists(STATE_FILE):
        backup = STATE_FILE + ".bak"
        shutil.copy2(STATE_FILE, backup)

    try:
        _save_state({"seen_urls": ["https://example.com/already-seen"]})

        fake_articles = [
            {"title": "Old", "url": "https://example.com/already-seen", "published": "", "snippet": ""},
            {"title": "Fresh", "url": "https://example.com/fresh", "published": "", "snippet": ""},
        ]

        import news_monitor as _nm
        with unittest.mock.patch.object(_nm, "_fetch_google_news_rss", return_value=fake_articles):
            result = check_news(keywords=["test"], mark_seen=True, ignore_seen=False)

        assert result["total_new"] == 1, f"Expected 1 new, got {result['total_new']}"
        assert result["new_articles"][0]["title"] == "Fresh"
        assert "all_articles" not in result, "Notify mode should not include all_articles"

        state_after = _load_state()
        assert "https://example.com/fresh" in state_after["seen_urls"], \
            "Notify mode must mark new URLs as seen"
    finally:
        if backup:
            shutil.move(backup, STATE_FILE)
        elif os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    print("\n-- test_notify_mode_still_filters_seen")
    print(f"   new_articles={result['total_new']} (correctly filtered)")
    print("   PASS")


async def test_news_agent_fallback_uses_research_mode() -> None:
    """news_agent _refresh_news_via_monitor calls check_news with ignore_seen=True."""
    import agents.news_agent as _mod

    with unittest.mock.patch("news_monitor.check_news") as mock_check:
        mock_check.return_value = {
            "all_articles": _EXAMPLE_ARTICLES[:2],
            "new_articles": [],
            "fetched_at_utc": "2026-05-16T12:00:00Z",
            "total_new": 0,
            "total_all": 2,
            "has_breaking": False,
            "data_source": "test",
            "checked_keywords": ["test"],
        }
        articles, error, updated_at = _mod._refresh_news_via_monitor()

    mock_check.assert_called_once_with(mark_seen=False, ignore_seen=True)
    assert len(articles) == 2, f"Expected 2 articles from all_articles, got {len(articles)}"
    assert error is None
    assert updated_at == "2026-05-16T12:00:00Z"

    print("\n-- test_news_agent_fallback_uses_research_mode")
    print(f"   check_news called with: {mock_check.call_args}")
    print(f"   returned {len(articles)} articles from all_articles")
    print("   PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 3b -- NewsAgent tests (mocked cache + LLM)")
    print("Phase 10.5.2 -- Research vs Notify mode tests")
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
    await test_stale_cache_refreshes()
    await test_stale_cache_no_recent_news()
    # Phase 10.5.2 tests
    await test_research_mode_returns_all_despite_seen()
    await test_research_mode_does_not_modify_seen_urls()
    await test_research_mode_updates_cache()
    await test_notify_mode_still_filters_seen()
    await test_news_agent_fallback_uses_research_mode()

    print("\n" + "=" * 60)
    print("All 16 tests passed.")


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
