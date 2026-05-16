#!/usr/bin/env python3
"""
Phase 9 Step 3c — MacroAgent standalone tests.

Uses mock search results — no network, no Tavily, no actual LLM calls.
All external I/O (_collect_search_results, _call_llm) is mocked.

Tests:
  1. test_all_queries_ok       — all 4 queries succeed + LLM JSON → ok, findings
  2. test_search_all_fail      — all queries fail → error/partial
  3. test_partial_queries      — 2 queries fail → partial, failed queries in missing_data
  4. test_llm_fallback         — search ok, LLM returns "" → raw titles, partial
  5. test_queries_preserved    — failed queries appear in missing_data
  6. test_sourceref_fields     — SourceRef: title, url, source, retrieved_at, published_at
  7. test_signal_directions    — LLM JSON directions map to Finding.direction
  8. test_json_safe            — output passes JSON-safety check
  9. test_banned_terms_and_fabricated_findings_sanitized

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research/agents
    python test_macro_agent.py
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
from agents.macro_agent import (  # noqa: E402
    MacroAgent, _MACRO_QUERIES, _MAX_CONFIDENCE,
    _stabilise_directions, _DIRECTIONAL_KEYS,
)


# ── Mock search results ───────────────────────────────────────────────────────

_TS = "2026-05-02T12:00:00+00:00"

_MOCK_RESULTS_FULL: list[dict] = [
    # RBA query
    {
        "title": "RBA holds cash rate at 4.35%, signals cautious outlook",
        "url": "https://example.com/macro/1",
        "snippet": "Reserve Bank of Australia kept interest rates unchanged, noting that inflation is moderating but still above target.",
        "source": "google_news_rss",
        "published_at": "2026-05-01T08:00:00Z",
        "query": "RBA Reserve Bank Australia interest rate 2025",
    },
    # PBoC query
    {
        "title": "PBoC cuts reserve ratio to support China economy",
        "url": "https://example.com/macro/2",
        "snippet": "People's Bank of China announced a 25bp cut to the reserve requirement ratio, signalling easing bias.",
        "source": "google_news_rss",
        "published_at": "2026-04-30T06:00:00Z",
        "query": "PBoC China central bank monetary policy yuan 2025",
    },
    # Fed/USD query
    {
        "title": "Fed holds rates, dollar strengthens on hawkish outlook",
        "url": "https://example.com/macro/3",
        "snippet": "Federal Reserve kept rates steady but reaffirmed commitment to fighting inflation, boosting USD.",
        "source": "tavily",
        "published_at": "2026-04-29T18:00:00Z",
        "query": "Federal Reserve USD dollar Australian dollar impact",
    },
    # AUD/CNY macro query
    {
        "title": "Australia-China trade tensions ease, AUD outlook improves",
        "url": "https://example.com/macro/4",
        "snippet": "Diplomatic progress between Australia and China supports trade flow and AUD sentiment.",
        "source": "google_news_rss",
        "published_at": "2026-04-28T10:00:00Z",
        "query": "Australia China trade AUD CNY macro outlook",
    },
]

# LLM JSON response based on the above results
_MOCK_LLM_JSON = json.dumps({
    "summary": "RBA持稳，PBoC宽松，美联储鹰派，澳中贸易改善，整体信号混杂",
    "rba_signal":  "neutral",
    "pboc_signal": "bullish_aud",
    "usd_signal":  "bearish_aud",
    "overall_direction": "mixed",
    "key_findings": [
        "RBA维持利率不变，通胀仍高于目标",
        "PBoC降准表明中国经济支持政策，利好大宗商品需求",
        "美联储鹰派立场使USD走强，短期压制AUD",
        "澳中贸易关系改善，中期利好AUD",
    ],
    "risks": [
        "美联储鹰派信号可能持续压制AUD",
        "中国经济数据存在下行不确定性",
    ],
    "data_gaps": [],
}, ensure_ascii=False)

_MOCK_TOKEN_USAGE = {"prompt_tokens": 480, "completion_tokens": 220}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_task() -> ResearchTask:
    return ResearchTask.from_preset(
        FX_CNYAUD_PRESET,
        safe_user_context=SafeUserContext(purpose="tuition"),
        focus_assets=["AUD", "CNY"],
        focus_pair="CNY/AUD",
    )


def _mock_search_ok(results: list | None = None):
    """Mock _collect_search_results to return full mock data."""
    import agents.macro_agent as _mod
    items = results if results is not None else _MOCK_RESULTS_FULL
    successful = list({r["query"] for r in items})
    failed: list[str] = []
    return unittest.mock.patch.object(
        _mod, "_collect_search_results",
        return_value=(items, successful, failed),
    )


def _mock_search_all_fail():
    import agents.macro_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_collect_search_results",
        return_value=([], [], list(_MACRO_QUERIES)),
    )


def _mock_search_partial(n_fail: int = 2):
    """First n_fail queries fail, rest succeed."""
    import agents.macro_agent as _mod
    results = _MOCK_RESULTS_FULL[n_fail:]   # only results from successful queries
    successful = [r["query"] for r in results]
    failed = list(_MACRO_QUERIES[:n_fail])
    return unittest.mock.patch.object(
        _mod, "_collect_search_results",
        return_value=(results, successful, failed),
    )


def _mock_llm_ok():
    import agents.macro_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_call_llm",
        return_value=(_MOCK_LLM_JSON, _MOCK_TOKEN_USAGE),
    )


def _mock_llm_fail():
    import agents.macro_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_call_llm",
        return_value=("", {}),
    )


def _mock_llm_text(text: str):
    import agents.macro_agent as _mod
    return unittest.mock.patch.object(
        _mod, "_call_llm",
        return_value=(text, _MOCK_TOKEN_USAGE),
    )


def _print_output(output: AgentOutput) -> None:
    print(f"   status={output.status}  conf={output.confidence:.2f}  "
          f"latency={output.latency_ms}ms  findings={len(output.findings)}")
    print(f"   summary: {output.summary[:70]}")
    for f in output.findings[:5]:
        print(f"   [{f.key:20s}] [{f.direction or 'none':14s}] {f.summary[:50]}")
    if output.risks:
        print(f"   risks: {output.risks[0][:70]}")
    if output.missing_data:
        print(f"   missing: {output.missing_data}")
    if output.token_usage:
        print(f"   token_usage: {output.token_usage}")


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_all_queries_ok() -> None:
    """All 4 queries succeed + LLM JSON → ok, signal findings, sources, token_usage."""
    with _mock_search_ok(), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    assert output.agent_name == "macro_agent"
    assert output.status in ("ok", "partial"), f"Expected ok/partial, got {output.status}"
    assert len(output.findings) >= 2, f"Expected ≥2 findings, got {len(output.findings)}"
    assert len(output.sources) > 0
    assert output.confidence > 0
    assert output.token_usage.get("prompt_tokens", 0) > 0

    # Signal findings from LLM JSON should be present
    keys = {f.key for f in output.findings}
    assert any(k.startswith("macro_") for k in keys), f"Expected macro_ findings, got: {keys}"
    json.dumps(output.to_dict(), ensure_ascii=False)

    print("\n-- test_all_queries_ok")
    _print_output(output)
    print("   PASS")


async def test_search_all_fail() -> None:
    """All queries fail → status=error or partial, all queries in missing_data."""
    with _mock_search_all_fail():
        output = await MacroAgent().run(_make_task())

    assert output.status in ("error", "partial"), f"Expected error/partial, got {output.status}"
    # All 4 failed queries should appear
    missing_text = " ".join(output.missing_data)
    assert len(output.missing_data) >= 4, (
        f"Expected ≥4 failed query entries in missing_data, got: {output.missing_data}"
    )
    json.dumps(output.to_dict(), ensure_ascii=False)

    print("\n-- test_search_all_fail")
    print(f"   status={output.status}  missing_data count={len(output.missing_data)}")
    print(f"   missing: {output.missing_data[:2]}")
    print("   PASS")


async def test_partial_queries() -> None:
    """2 queries fail → partial, failed query names appear in missing_data."""
    with _mock_search_partial(n_fail=2), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    assert output.status == "partial", f"Expected partial, got {output.status}"
    failed_in_missing = [m for m in output.missing_data if m.startswith("search_failed:")]
    assert len(failed_in_missing) == 2, (
        f"Expected 2 search_failed entries, got: {failed_in_missing}"
    )

    print("\n-- test_partial_queries")
    _print_output(output)
    print("   PASS")


async def test_llm_fallback() -> None:
    """Search ok, LLM returns "" → raw titles as findings, partial, token_usage estimated."""
    with _mock_search_ok(), _mock_llm_fail():
        output = await MacroAgent().run(_make_task())

    assert output.status == "partial", f"Expected partial (LLM fallback), got {output.status}"
    assert any("llm_unavailable" in m for m in output.missing_data), (
        f"Expected llm_unavailable in missing_data, got: {output.missing_data}"
    )
    assert len(output.findings) > 0, "Expected raw-title findings as fallback"
    assert "prompt_tokens" in output.token_usage, (
        f"Expected estimated prompt_tokens, got: {output.token_usage}"
    )

    print("\n-- test_llm_fallback")
    _print_output(output)
    print("   PASS")


async def test_queries_preserved() -> None:
    """Failed query names are preserved in missing_data with 'search_failed:' prefix."""
    with _mock_search_partial(n_fail=3), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    failed_entries = [m for m in output.missing_data if m.startswith("search_failed:")]
    assert len(failed_entries) == 3, (
        f"Expected 3 search_failed entries, got: {failed_entries}"
    )
    # The actual query text should be in the missing_data entry
    for entry in failed_entries:
        assert len(entry) > len("search_failed: "), f"Query text missing from: {entry}"

    print("\n-- test_queries_preserved")
    print(f"   failed queries in missing_data: {failed_entries}")
    print("   PASS")


async def test_sourceref_fields() -> None:
    """SourceRef has all required fields: title, url, source, retrieved_at, published_at."""
    with _mock_search_ok(), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    assert len(output.sources) > 0, "Expected at least one source"
    for src in output.sources:
        assert src.title,        f"SourceRef missing title: {src}"
        assert src.url,          f"SourceRef missing url: {src}"
        assert src.source,       f"SourceRef missing source: {src}"
        assert src.retrieved_at, f"SourceRef missing retrieved_at: {src}"
        # published_at may be None but field must exist (dataclass default is None)
        assert hasattr(src, "published_at")

    print("\n-- test_sourceref_fields")
    print(f"   {len(output.sources)} sources, all required fields present")
    for s in output.sources[:2]:
        print(f"   source={s.source}  published_at={s.published_at}")
    print("   PASS")


async def test_signal_directions() -> None:
    """LLM signal fields map correctly to Finding.direction values."""
    with _mock_search_ok(), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    # From _MOCK_LLM_JSON:
    # rba_signal=neutral, pboc_signal=bullish_aud, usd_signal=bearish_aud
    findings_by_key = {f.key: f for f in output.findings}

    if "macro_rba" in findings_by_key:
        assert findings_by_key["macro_rba"].direction == "neutral", (
            f"RBA signal should be neutral, got: {findings_by_key['macro_rba'].direction}"
        )
    if "macro_pboc" in findings_by_key:
        assert findings_by_key["macro_pboc"].direction == "bullish_aud", (
            f"PBoC signal should be bullish_aud, got: {findings_by_key['macro_pboc'].direction}"
        )
    if "macro_usd" in findings_by_key:
        assert findings_by_key["macro_usd"].direction == "bearish_aud", (
            f"USD signal should be bearish_aud, got: {findings_by_key['macro_usd'].direction}"
        )

    print("\n-- test_signal_directions")
    for key in ("macro_rba", "macro_pboc", "macro_usd"):
        if key in findings_by_key:
            print(f"   [{key}] direction={findings_by_key[key].direction}")
    print("   PASS")


async def test_signal_source_ids_are_compact() -> None:
    """Macro signal findings should not attach every retrieved source."""
    with _mock_search_ok(), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    findings_by_key = {f.key: f for f in output.findings}
    assert findings_by_key["macro_rba"].source_ids == ["https://example.com/macro/1"]
    assert findings_by_key["macro_pboc"].source_ids == ["https://example.com/macro/2"]
    assert findings_by_key["macro_usd"].source_ids == ["https://example.com/macro/3"]

    print("\n-- test_signal_source_ids_are_compact")
    for key in ("macro_rba", "macro_pboc", "macro_usd"):
        print(f"   [{key}] sources={findings_by_key[key].source_ids}")
    print("   PASS")


async def test_json_safe() -> None:
    """Output serialises to valid JSON in all paths."""
    with _mock_search_all_fail():
        out1 = await MacroAgent().run(_make_task())
    json.dumps(out1.to_dict(), ensure_ascii=False)

    with _mock_search_ok(), _mock_llm_ok():
        out2 = await MacroAgent().run(_make_task())
    json.dumps(out2.to_dict(), ensure_ascii=False)

    with _mock_search_ok(), _mock_llm_fail():
        out3 = await MacroAgent().run(_make_task())
    json.dumps(out3.to_dict(), ensure_ascii=False)

    print("\n-- test_json_safe")
    print(f"   search_fail: {len(json.dumps(out1.to_dict()))} chars")
    print(f"   llm_ok:      {len(json.dumps(out2.to_dict()))} chars")
    print(f"   llm_fallback:{len(json.dumps(out3.to_dict()))} chars")
    print("   PASS")


async def test_banned_terms_and_fabricated_findings_sanitized() -> None:
    """LLM banned terms are removed and detail findings use search titles."""
    unsafe = json.dumps({
        "summary": "最佳时机，建议买入",
        "rba_signal": "bullish_aud",
        "pboc_signal": "neutral",
        "usd_signal": "bearish_aud",
        "overall_direction": "bullish_aud",
        "key_findings": ["编造一个未检索到的事实"],
        "risks": ["应该买这类建议不应保留"],
        "data_gaps": [],
    }, ensure_ascii=False)
    with _mock_search_ok(), _mock_llm_text(unsafe):
        output = await MacroAgent().run(_make_task())

    all_text = output.summary + " ".join(output.risks)
    for term in FX_CNYAUD_PRESET.banned_terms:
        assert term not in all_text, f"Banned term {term!r} found in output"
    assert "unsafe_llm_terms_removed" in output.missing_data
    assert not any("编造一个未检索到的事实" in f.summary for f in output.findings)
    titles = {r["title"] for r in _MOCK_RESULTS_FULL}
    assert any(f.summary in titles for f in output.findings)

    print("\n-- test_banned_terms_and_fabricated_findings_sanitized")
    print(f"   summary: {output.summary}")
    print("   PASS")


# ── Phase 10.5.1C — direction stabilisation tests ────────────────────────────

async def test_detail_direction_cleared() -> None:
    """macro_detail_* findings must have direction=None even when LLM sets overall_direction."""
    llm_json = json.dumps({
        "summary": "整体偏多AUD",
        "rba_signal": "bullish_aud",
        "pboc_signal": "neutral",
        "usd_signal": "bullish_aud",
        "overall_direction": "bullish_aud",
        "key_findings": [],
        "risks": [],
        "data_gaps": [],
    }, ensure_ascii=False)
    with _mock_search_ok(), _mock_llm_text(llm_json):
        output = await MacroAgent().run(_make_task())

    details = [f for f in output.findings if f.key.startswith("macro_detail_")]
    assert len(details) > 0, "Expected macro_detail findings"
    for f in details:
        assert f.direction is None, (
            f"{f.key} should have direction=None, got {f.direction!r}"
        )

    print("\n-- test_detail_direction_cleared")
    print(f"   {len(details)} detail findings, all direction=None")
    print("   PASS")


async def test_macro_usd_weak_evidence() -> None:
    """macro_usd with no USD-related search results → direction=None + data_gap."""
    no_usd_results = [
        {
            "title": "RBA holds cash rate at 4.35%",
            "url": "https://example.com/rba",
            "snippet": "Reserve Bank of Australia kept rates unchanged.",
            "source": "google_news_rss",
            "query": "RBA interest rate",
        },
        {
            "title": "PBoC cuts reserve ratio",
            "url": "https://example.com/pboc",
            "snippet": "People's Bank of China easing.",
            "source": "google_news_rss",
            "query": "PBoC monetary policy",
        },
    ]
    llm_json = json.dumps({
        "summary": "RBA持稳，PBoC宽松",
        "rba_signal": "neutral",
        "pboc_signal": "bullish_aud",
        "usd_signal": "bearish_aud",
        "overall_direction": "bullish_aud",
        "key_findings": [],
        "risks": [],
        "data_gaps": [],
    }, ensure_ascii=False)
    with _mock_search_ok(no_usd_results), _mock_llm_text(llm_json):
        output = await MacroAgent().run(_make_task())

    usd_findings = [f for f in output.findings if f.key == "macro_usd"]
    assert len(usd_findings) == 1
    assert usd_findings[0].direction is None, (
        f"macro_usd without USD evidence should be None, got {usd_findings[0].direction!r}"
    )
    assert "macro_usd_no_source_evidence" in output.missing_data

    print("\n-- test_macro_usd_weak_evidence")
    print(f"   macro_usd direction={usd_findings[0].direction}")
    print(f"   missing_data includes data_gap: {'macro_usd_no_source_evidence' in output.missing_data}")
    print("   PASS")


async def test_rba_pboc_directions_preserved() -> None:
    """High-level RBA/PBoC directions are preserved when evidence exists."""
    with _mock_search_ok(), _mock_llm_ok():
        output = await MacroAgent().run(_make_task())

    by_key = {f.key: f for f in output.findings}
    if "macro_rba" in by_key:
        assert by_key["macro_rba"].direction == "neutral"
    if "macro_pboc" in by_key:
        assert by_key["macro_pboc"].direction == "bullish_aud"

    print("\n-- test_rba_pboc_directions_preserved")
    for k in ("macro_rba", "macro_pboc"):
        if k in by_key:
            print(f"   [{k}] direction={by_key[k].direction}")
    print("   PASS")


async def test_risk_agent_direction_count_stable() -> None:
    """With same mock input, macro output direction counts are deterministic for RiskAgent."""
    counts = []
    for _ in range(3):
        with _mock_search_ok(), _mock_llm_ok():
            output = await MacroAgent().run(_make_task())
        bull = sum(1 for f in output.findings if f.direction == "bullish_aud")
        bear = sum(1 for f in output.findings if f.direction == "bearish_aud")
        counts.append((bull, bear))

    assert all(c == counts[0] for c in counts), (
        f"Direction counts should be stable across runs, got {counts}"
    )
    # With stabilisation: detail findings have no direction,
    # so only signal findings (rba=neutral, pboc=bullish_aud, usd=bearish_aud) count.
    bull, bear = counts[0]
    assert bull == 1, f"Expected 1 bullish (pboc), got {bull}"
    assert bear == 1, f"Expected 1 bearish (usd), got {bear}"

    print("\n-- test_risk_agent_direction_count_stable")
    print(f"   direction counts (bullish, bearish) stable: {counts[0]}")
    print("   PASS")


def test_stabilise_directions_unit() -> None:
    """Unit test for _stabilise_directions function."""
    from schema import Finding
    findings = [
        Finding(key="macro_rba", summary="RBA", direction="bullish_aud"),
        Finding(key="macro_usd", summary="USD", direction="bearish_aud"),
        Finding(key="macro_detail_0", summary="headline", direction="bullish_aud"),
        Finding(key="macro_detail_1", summary="headline2", direction="neutral"),
        Finding(key="macro_raw_0", summary="raw", direction="bearish_aud"),
        Finding(key="bad_key", summary="unknown", direction="invalid_value"),
    ]
    results_with_usd = [{"title": "Fed holds rates", "query": "Federal Reserve USD"}]
    results_no_usd = [{"title": "RBA holds", "query": "RBA rates"}]

    stable, gaps = _stabilise_directions(findings, results_with_usd)
    by_key = {f.key: f for f in stable}
    assert by_key["macro_rba"].direction == "bullish_aud"
    assert by_key["macro_usd"].direction == "bearish_aud"
    assert by_key["macro_detail_0"].direction is None
    assert by_key["macro_detail_1"].direction is None
    assert by_key["macro_raw_0"].direction is None
    assert by_key["bad_key"].direction is None
    assert gaps == []

    stable2, gaps2 = _stabilise_directions(findings, results_no_usd)
    by_key2 = {f.key: f for f in stable2}
    assert by_key2["macro_usd"].direction is None
    assert "macro_usd_no_source_evidence" in gaps2

    print("\n-- test_stabilise_directions_unit")
    print("   detail/raw directions cleared, USD without evidence → None")
    print("   PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 3c + 10.5.1C -- MacroAgent tests (mocked search + LLM)")
    print(f"Queries: {_MACRO_QUERIES}")
    print("=" * 60)

    await test_all_queries_ok()
    await test_search_all_fail()
    await test_partial_queries()
    await test_llm_fallback()
    await test_queries_preserved()
    await test_sourceref_fields()
    await test_signal_directions()
    await test_signal_source_ids_are_compact()
    await test_json_safe()
    await test_banned_terms_and_fabricated_findings_sanitized()
    # Phase 10.5.1C — direction stabilisation
    await test_detail_direction_cleared()
    await test_macro_usd_weak_evidence()
    await test_rba_pboc_directions_preserved()
    await test_risk_agent_direction_count_stable()
    test_stabilise_directions_unit()

    print("\n" + "=" * 60)
    print("All 15 tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        MacroAgent.close_executor()
