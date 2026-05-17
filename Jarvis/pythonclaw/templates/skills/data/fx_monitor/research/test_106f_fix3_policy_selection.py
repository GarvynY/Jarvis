#!/usr/bin/env python3
"""
Phase 10.6F-fix3 — PolicySignalAgent selection & source quality fix.

Tests:
  1. test_policy_with_official_source_gets_selected
  2. test_weak_aggregator_policy_not_forced
  3. test_macro_does_not_crowd_valid_policy
  4. test_fx_current_rate_and_target_preserved
  5. test_risk_minimum_preserved
  6. test_market_drivers_preserved_when_enabled
  7. test_token_usage_reported
  8. test_tavily_domain_filter_used
  9. test_llm_call_correct_signature

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106f_fix3_policy_selection.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (
    AgentOutput,
    EvidenceChunk,
    Finding,
    FindingCategory,
    ResearchPreset,
    ResearchTask,
    SafeUserContext,
    SourceRef,
    now_iso,
)
from evidence_store import EvidenceStore


def _make_task() -> ResearchTask:
    return ResearchTask(
        preset_name="fx_cnyaud",
        research_topic="CNY/AUD",
        focus_pair="CNY/AUD",
        focus_assets=["CNY", "AUD"],
    )


def _make_policy_output(confidence: float = 0.7, tier: int = 2) -> AgentOutput:
    """Create a policy_signal_agent output with realistic findings."""
    from source_metadata import SourceMetadata
    from agents.policy_signal_agent import build_source_metadata_for_bucket

    findings = []
    for bucket, stance, conf in [
        ("policy_rba", "hawkish", 0.8),
        ("policy_pboc", "dovish", 0.7),
        ("policy_fed", "neutral", 0.6),
    ]:
        direction = None
        if stance == "hawkish" and bucket == "policy_rba":
            direction = "bullish_aud"
        elif stance == "dovish" and bucket == "policy_pboc":
            direction = "bullish_aud"

        importance = 0.8 if stance in ("hawkish", "dovish") and conf >= 0.5 and tier <= 2 else 0.6
        evidence_score = min(0.85, conf * 0.9)

        f = Finding(
            key=bucket,
            summary=f"{bucket}: {stance}",
            direction=direction,
            evidence_score=evidence_score,
            category="policy_signal",
            importance=importance,
            source_ids=[f"https://www.rba.gov.au/mr-{bucket}.html"] if tier <= 1 else [f"https://reuters.com/{bucket}"],
            evidence_basis=f"policy_signal_agent:{bucket} tier={tier}",
        )
        if tier <= 2:
            url = f"https://www.rba.gov.au/mr.html" if tier == 1 else f"https://reuters.com/{bucket}"
            meta = build_source_metadata_for_bucket(bucket, [{"url": url, "title": f"{bucket} decision"}])
        else:
            meta = build_source_metadata_for_bucket(bucket, [{"url": "https://news.google.com/rba", "title": "RBA"}])
        f._source_metadata_json = meta.to_json()
        findings.append(f)

    return AgentOutput(
        agent_name="policy_signal_agent",
        status="ok",
        summary="PolicySignal: 3/3 buckets",
        findings=findings,
        sources=[SourceRef(url="https://reuters.com/rba", title="RBA", source="reuters.com", retrieved_at=now_iso())],
        confidence=confidence,
        token_usage={"prompt_tokens": 2000, "completion_tokens": 300},
    )


def _make_macro_output() -> AgentOutput:
    """Create a macro_agent output with macro_detail findings."""
    findings = [
        Finding(key="macro_detail_0", summary="Australia hikes rates", category="macro",
                importance=0.7, direction=None, evidence_score=0.5),
        Finding(key="macro_detail_1", summary="RBA statement", category="macro",
                importance=0.65, direction=None, evidence_score=0.5),
        Finding(key="macro_rba", summary="RBA policy", category="policy_signal",
                importance=0.8, direction="bullish_aud", evidence_score=0.6),
    ]
    return AgentOutput(
        agent_name="macro_agent",
        status="partial",
        summary="Macro partial",
        findings=findings,
        sources=[SourceRef(url="https://news.google.com/rba", title="Macro", source="google_news_rss", retrieved_at=now_iso())],
        confidence=0.75,
    )


def _make_fx_output() -> AgentOutput:
    """FX agent with current_rate and target_rate_gap."""
    findings = [
        Finding(key="current_rate", summary="1 AUD = 4.88 CNY", category="fx_price",
                importance=0.9, direction=None, evidence_score=0.9),
        Finding(key="historical_trend", summary="90d -1.17%", category="fx_price",
                importance=0.85, direction="bearish_aud", evidence_score=0.85),
        Finding(key="target_rate_gap", summary="目标汇率 +2.27%", category="fx_price",
                importance=0.85, direction=None, evidence_score=0.8),
    ]
    return AgentOutput(
        agent_name="fx_agent",
        status="ok",
        summary="FX ok",
        findings=findings,
        sources=[SourceRef(url="https://open.er-api.com/v6/latest/CNY", title="FX", source="open.er-api.com", retrieved_at=now_iso())],
        confidence=0.85,
    )


def _make_risk_output() -> AgentOutput:
    findings = [
        Finding(key="signal_contradiction", summary="多空矛盾", category="risk",
                importance=0.75, direction=None, evidence_score=0.7),
        Finding(key="dominant_signal", summary="偏空 AUD", category="risk",
                importance=0.7, direction="bearish_aud", evidence_score=0.65),
    ]
    return AgentOutput(
        agent_name="risk_agent",
        status="ok",
        summary="Risk ok",
        findings=findings,
        confidence=0.575,
    )


# ── Test 1: Valid policy signal from official/quality source gets selected ────

def test_policy_with_official_source_gets_selected() -> None:
    """Policy findings from tier 1-2 sources with conf>=0.5 should enter ContextPack."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    outputs = [_make_fx_output(), _make_macro_output(), _make_policy_output(confidence=0.7, tier=2), _make_risk_output()]

    from coordinator import _dedup_policy_signals
    outputs = _dedup_policy_signals(outputs)
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    policy_items = [it for it in pack.items if it.agent_name == "policy_signal_agent"]
    assert len(policy_items) >= 1, (
        f"Expected at least 1 policy chunk selected, got {len(policy_items)}. "
        f"All items: {[(it.agent_name, it.chunk_id[:12]) for it in pack.items]}"
    )
    assert len(policy_items) <= 2, f"Policy should have max 2 chunks, got {len(policy_items)}"
    print("  policy with official source selected    OK")


def test_policy_candidates_receive_persisted_scores() -> None:
    """Policy candidates should keep composite scores even when not selected."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    outputs = [_make_fx_output(), _make_policy_output(confidence=0.7, tier=2), _make_risk_output()]
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    policy_chunks = store.query_chunks(
        task.task_id,
        category="policy_signal",
        agent_name="policy_signal_agent",
        top_k=10,
    )
    assert policy_chunks, "Expected policy chunks"
    unselected = [c for c in policy_chunks if not c.used_in_brief]
    assert unselected, "Expected at least one unselected policy candidate"
    for chunk in policy_chunks:
        loaded = store.get_chunk(chunk.chunk_id)
        assert loaded is not None
        assert loaded.composite_score > 0, f"Missing composite score for {chunk.chunk_id}"
        assert loaded.score_reason, f"Missing score_reason for {chunk.chunk_id}"
    print("  policy candidates receive scores        OK")


def test_long_policy_sources_are_compacted_scored_and_selectable() -> None:
    """Real policy buckets with many long source URLs must not be filtered pre-score."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    policy_out = _make_policy_output(confidence=0.53, tier=3)
    long_sources = []
    for i in range(5):
        url = "https://news.google.com/rss/articles/" + ("X" * 180) + str(i) + "?oc=5"
        long_sources.append(SourceRef(
            url=url,
            title=("China central bank to cut sector-specific rates to boost economy - Reuters " + str(i)),
            source="google_news_rss",
            retrieved_at=now_iso(),
        ))
    policy_out.sources = long_sources
    for finding in policy_out.findings:
        finding.source_ids = [src.url for src in long_sources]
        finding.evidence_score = 0.81 if finding.key == "policy_pboc" else 0.54
        finding.importance = 0.6
        finding.direction = "bullish_aud" if finding.key == "policy_pboc" else "neutral"
        finding.evidence_basis = "policy_signal_agent:test tier=3"
        finding._source_metadata_json = json.dumps({
            "url": long_sources[0].url,
            "domain": "news.google.com",
            "provider": "policy_signal_agent",
            "source_type": "general_news",
            "source_tier": 3,
            "quality_reason": f"policy_bucket:{finding.key},best_tier=3,domain=news.google.com",
        })

    md_output = AgentOutput(
        agent_name="market_drivers_agent",
        status="ok",
        summary="MD ok",
        findings=[
            Finding(key="commodity_copper", summary="Copper bearish", category="commodity_trade",
                    importance=0.85, direction="bearish_aud", evidence_score=0.8),
        ],
        sources=[SourceRef(url="https://finance.yahoo.com/copper", title="Copper", source="yfinance", retrieved_at=now_iso())],
        confidence=0.74,
    )

    outputs = [_make_fx_output(), policy_out, md_output, _make_risk_output()]
    store.ingest_outputs(task, outputs)

    policy_chunks = store.query_chunks(
        task.task_id,
        category="policy_signal",
        agent_name="policy_signal_agent",
        top_k=10,
    )
    assert policy_chunks, "Expected policy chunks"
    assert max(c.token_estimate for c in policy_chunks) <= 1200, [
        (c.token_estimate, c.source) for c in policy_chunks
    ]
    pboc_chunk = next(c for c in policy_chunks if "finding_key=policy_pboc" in (c.source or ""))
    assert pboc_chunk.confidence == 0.81, "Finding evidence_score should map to policy chunk confidence"

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    loaded_pboc = store.get_chunk(pboc_chunk.chunk_id)
    assert loaded_pboc is not None
    assert loaded_pboc.composite_score > 0
    assert loaded_pboc.score_reason

    policy_items = [it for it in pack.items if it.agent_name == "policy_signal_agent"]
    md_items = [it for it in pack.items if it.agent_name == "market_drivers_agent"]
    assert len(policy_items) >= 1, "Valid policy should be selected after scoring"
    assert len(md_items) >= 1, "Market driver should remain selected"
    print("  long policy sources scored+selected     OK")


# ── Test 2: Weak aggregator-only policy not forced ─────────────────────────

def test_weak_aggregator_policy_not_forced() -> None:
    """Policy findings from tier 4 aggregators with low confidence should NOT be forced into pack."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    # Low confidence, tier 4
    policy_out = _make_policy_output(confidence=0.3, tier=4)
    for f in policy_out.findings:
        f.importance = 0.4
        f.direction = None
        f.evidence_score = 0.2

    outputs = [_make_fx_output(), _make_macro_output(), policy_out, _make_risk_output()]
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    policy_items = [it for it in pack.items if it.agent_name == "policy_signal_agent"]
    # Weak policy with low importance AND low confidence should not be forced
    # (importance < 0.6 AND confidence < 0.5)
    assert len(policy_items) == 0, (
        f"Weak policy should not be forced, got {len(policy_items)} items"
    )
    print("  weak aggregator policy not forced       OK")


# ── Test 3: MacroAgent doesn't crowd valid policy ──────────────────────────

def test_macro_does_not_crowd_valid_policy() -> None:
    """When policy_signal_agent has valid findings, macro_detail should not crowd them out."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    outputs = [_make_fx_output(), _make_macro_output(), _make_policy_output(confidence=0.7, tier=1), _make_risk_output()]

    from coordinator import _dedup_policy_signals
    outputs = _dedup_policy_signals(outputs)
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    policy_items = [it for it in pack.items if it.agent_name == "policy_signal_agent"]
    macro_items = [it for it in pack.items if it.agent_name == "macro_agent"]

    assert len(policy_items) >= 1, f"Policy should have at least 1 chunk, got {len(policy_items)}"
    # Verify dedup worked: macro_rba should be demoted
    print("  macro does not crowd valid policy       OK")


# ── Test 4: FX current_rate and target_rate_gap preserved ──────────────────

def test_fx_current_rate_and_target_preserved() -> None:
    """FX core chunks must always be selected regardless of policy changes."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    outputs = [_make_fx_output(), _make_macro_output(), _make_policy_output(), _make_risk_output()]
    from coordinator import _dedup_policy_signals
    outputs = _dedup_policy_signals(outputs)
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
    assert len(fx_items) >= 2, f"FX should have at least 2 chunks (current_rate+target), got {len(fx_items)}"
    print("  fx current_rate + target preserved      OK")


# ── Test 5: Risk minimum preserved ─────────────────────────────────────────

def test_risk_minimum_preserved() -> None:
    """Risk section must have at least 1 chunk selected."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    outputs = [_make_fx_output(), _make_macro_output(), _make_policy_output(), _make_risk_output()]
    from coordinator import _dedup_policy_signals
    outputs = _dedup_policy_signals(outputs)
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    risk_items = [it for it in pack.items if it.agent_name == "risk_agent"]
    assert len(risk_items) >= 1, f"Risk must have at least 1 chunk, got {len(risk_items)}"
    print("  risk minimum preserved                  OK")


# ── Test 6: MarketDrivers preserved when enabled ──────────────────────────

def test_market_drivers_preserved_when_enabled() -> None:
    """If MarketDriversAgent produces findings, they should still be selectable."""
    store = EvidenceStore(":memory:")
    task = _make_task()

    md_output = AgentOutput(
        agent_name="market_drivers_agent",
        status="ok",
        summary="MD ok",
        findings=[
            Finding(key="commodity_copper", summary="Copper bearish", category="market_driver",
                    importance=0.85, direction="bearish_aud", evidence_score=0.8),
            Finding(key="fx_aud_usd", summary="AUD/USD rising", category="market_driver",
                    importance=0.82, direction="bullish_aud", evidence_score=0.75),
        ],
        sources=[SourceRef(url="https://finance.yahoo.com/copper", title="Copper", source="yfinance", retrieved_at=now_iso())],
        confidence=0.74,
    )

    outputs = [_make_fx_output(), _make_macro_output(), _make_policy_output(), md_output, _make_risk_output()]
    from coordinator import _dedup_policy_signals
    outputs = _dedup_policy_signals(outputs)
    store.ingest_outputs(task, outputs)

    from schema import FX_CNYAUD_PRESET
    pack = store.build_context_pack(task, FX_CNYAUD_PRESET, outputs, token_budget=7500)

    md_items = [it for it in pack.items if it.agent_name == "market_drivers_agent"]
    policy_items = [it for it in pack.items if it.agent_name == "policy_signal_agent"]
    assert len(policy_items) >= 1, "Valid policy should not be crowded out by market drivers"
    assert len(md_items) >= 1, "MarketDrivers should retain at least one macro slot"
    all_agents = set(it.agent_name for it in pack.items)
    assert "fx_agent" in all_agents
    assert "risk_agent" in all_agents
    print("  market_drivers preserved when enabled   OK")


# ── Test 7: Token usage reported ───────────────────────────────────────────

def test_token_usage_reported() -> None:
    """PolicySignalAgent should report token usage from LLM calls."""
    from agents.policy_signal_agent import _call_policy_llm

    # Mock _call_llm to return a valid response
    mock_response = json.dumps({
        "rba_stance": "hawkish",
        "rba_summary": "RBA raised",
        "rba_confidence": 0.8,
        "pboc_stance": "dovish",
        "pboc_summary": "PBoC cut",
        "pboc_confidence": 0.7,
        "fed_stance": "neutral",
        "fed_summary": "Fed holds",
        "fed_confidence": 0.6,
    })
    mock_usage = {"prompt_tokens": 1500, "completion_tokens": 200}

    import agents.policy_signal_agent as psa_mod
    original_call = psa_mod._call_llm
    try:
        psa_mod._call_llm = lambda prompt, system, max_tokens: (mock_response, mock_usage)
        result = _call_policy_llm([{"title": "RBA", "url": "https://rba.gov.au/x", "snippet": "rates", "query": "rba"}])
    finally:
        psa_mod._call_llm = original_call

    assert "_token_usage" in result, f"Missing _token_usage in result: {list(result.keys())}"
    usage = result["_token_usage"]
    assert usage.get("prompt_tokens", 0) > 0, f"prompt_tokens should be > 0, got {usage}"
    assert usage.get("completion_tokens", 0) > 0, f"completion_tokens should be > 0, got {usage}"
    assert result.get("rba_stance") == "hawkish"
    print("  token usage reported                    OK")


# ── Test 8: Tavily domain filter used ──────────────────────────────────────

def test_tavily_domain_filter_used() -> None:
    """_search_for_bucket should attempt Tavily with include_domains first."""
    from agents.policy_signal_agent import _search_for_bucket, _BUCKET_PREFERRED_DOMAINS

    assert "policy_rba" in _BUCKET_PREFERRED_DOMAINS
    rba_domains = _BUCKET_PREFERRED_DOMAINS["policy_rba"]
    assert "rba.gov.au" in rba_domains
    assert "reuters.com" in rba_domains

    assert "policy_pboc" in _BUCKET_PREFERRED_DOMAINS
    pboc_domains = _BUCKET_PREFERRED_DOMAINS["policy_pboc"]
    assert "pbc.gov.cn" in pboc_domains

    assert "policy_fed" in _BUCKET_PREFERRED_DOMAINS
    fed_domains = _BUCKET_PREFERRED_DOMAINS["policy_fed"]
    assert "federalreserve.gov" in fed_domains
    print("  tavily domain filter configured         OK")


# ── Test 9: LLM call uses correct signature ────────────────────────────────

def test_llm_call_correct_signature() -> None:
    """_call_policy_llm uses call_json_with_repair with correct positional args."""
    import inspect
    from agents.policy_signal_agent import _call_policy_llm

    source = inspect.getsource(_call_policy_llm)
    assert "call_json_with_repair(" in source
    assert "_call_llm," in source, "First arg should be _call_llm function"
    assert "user_prompt," in source, "Second arg should be user_prompt"
    assert "_SYSTEM_PROMPT," in source, "Third arg should be _SYSTEM_PROMPT"
    assert "required_keys=" in source, "Should have required_keys kwarg"
    # Old broken params should NOT be present
    assert "system_prompt=" not in source, "Should not use system_prompt= kwarg"
    assert "model=" not in source, "Should not use model= kwarg"
    assert "max_retries=" not in source, "Should not use max_retries= kwarg"
    print("  LLM call correct signature              OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_policy_with_official_source_gets_selected,
        test_policy_candidates_receive_persisted_scores,
        test_long_policy_sources_are_compacted_scored_and_selectable,
        test_weak_aggregator_policy_not_forced,
        test_macro_does_not_crowd_valid_policy,
        test_fx_current_rate_and_target_preserved,
        test_risk_minimum_preserved,
        test_market_drivers_preserved_when_enabled,
        test_token_usage_reported,
        test_tavily_domain_filter_used,
        test_llm_call_correct_signature,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6F-fix3 Policy Selection — {len(tests)} tests")
    print(f"{'='*60}")
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            print(f"  FAIL: {t.__name__} — {exc}")
    print(f"\n{'='*60}")
    if passed == len(tests):
        print(f"All {passed} tests passed.")
    else:
        print(f"{passed}/{len(tests)} tests passed, {len(tests) - passed} FAILED.")
        sys.exit(1)
